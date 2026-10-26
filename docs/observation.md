# 可信观察登记与坐标校验

这一层解决的是「视觉模型看到了按钮」和「佳真的去点按钮」之间的信任问题。
**它本身不发送任何鼠标或键盘事件**，只回答一个问题：这次观察里的坐标，
现在还能不能信？

代码位置：

- `app/vision/observation.py`：记录结构、登记处、校验入口
- `app/tools/vision_tools.py`：`inspect_screen` 成功后登记观察
- `config/tools.yaml`：`vision.observation_ttl_seconds`
- `scripts/test_observation.py`：离线测试

## 一、什么时候产生一条记录

只有在下面这条路径完整走通之后，服务端才登记一条观察：

```
用户确认
  -> 真实捕获（Windows Graphics Capture 单窗口 / 整桌面）
  -> 捕获后再次核对窗口身份
  -> 视觉模型分析
  -> 建立观察记录
```

任何一步失败都不会留下记录。视觉模型自己不能创建记录，也不能指定
`observation_id` 或元素编号。

## 二、记录里有什么

| 字段 | 含义 |
| --- | --- |
| `observation_id` | 服务端生成的唯一编号，形如 `obs_...` |
| `scope` | `window`（单窗口）或 `desktop`（整桌面） |
| `window` | 捕获时的窗口身份：HWND、PID、进程、标题、窗口类 |
| `window_rect` | 捕获时的窗口物理像素矩形 |
| `captured_at` / `expires_at` | 截图时间与失效时间 |
| `ttl_seconds` | 有效期，默认 30 秒 |
| `image_size` / `preview_size` | 截图尺寸和送给模型的预览尺寸 |
| `content_size` | 捕获组件报告的窗口内容尺寸 |
| `image_digest` | 预览图的 SHA-256 指纹；**不保存图像本身** |
| `metrics` | DPI、缩放、虚拟桌面原点/尺寸、显示器数量、感知级别 |
| `elements` | 服务端编号的元素：`e1`、`e2`……，含名称、类型、中心点和边界框 |
| `operable` | 这条记录能不能用于定位操作 |
| `mapping_problem` | 不能用于定位时的具体原因 |

截图仍然只在内存里活到分析结束。记录只保留几何信息和指纹。

## 三、坐标怎么映射

视觉模型给的是相对「它看到的那张预览图」的 0~1 坐标。记录同时保存
预览来源的截图尺寸和窗口矩形，校验时：

```
归一化坐标
  -> 截图像素（按 image_size）
  -> 屏幕物理像素（按 window_rect）
```

不假设「预览图像素 = 屏幕像素」。窗口从 100,50 移到 110,50，即使尺寸没变，
旧记录也会被拒绝。

## 四、校验会检查什么

独立入口：

```python
from app.vision.observation import validate_click_target

result = validate_click_target(observation_id, element_id, x=None, y=None)
if not result.ok:
    # result.code / result.reason 说明原因
    ...
else:
    # result.screen_x / result.screen_y 是映射后的物理坐标
    ...
```

检查顺序和失败原因码：

| 检查 | 失败码 |
| --- | --- |
| 参数是合法字符串 | `invalid_arguments` |
| 记录存在 | `observation_not_found` |
| 记录未被主动失效 | `observation_invalidated` |
| 记录未过期 | `observation_expired` |
| 是单窗口观察 | `scope_not_operable` |
| 坐标映射可信（DPI 感知、截图尺寸、窗口身份齐全） | `mapping_not_trustworthy` |
| 元素确实属于这条记录 | `element_not_found` |
| x/y 同时提供、是有限数字 | `invalid_coordinates` |
| 坐标在 0~1 内 | `coordinates_out_of_bounds` |
| 坐标落在元素可信边界内 | `coordinates_mismatch` |
| 窗口仍然存在、可见、未最小化 | `window_gone` / `window_not_visible` / `window_minimized` |
| 窗口身份未变（句柄、PID、进程、标题、窗口类） | `window_changed` |
| 窗口位置未变 | `window_moved` |
| 窗口尺寸未变 | `window_resized` |
| DPI / 屏幕度量未变 | `environment_changed` |
| 映射后的点仍在窗口和桌面范围内 | `point_out_of_bounds` |
| 未预期异常 | `internal_error` |

**失败时 `screen_x` / `screen_y` 一定是 `None`。** 校验不会猜一个新坐标，
也不会退回旧截图继续用。

如果不传 x/y，使用元素中心点；如果传了，必须落在该元素的可信边界框内
（允许 0.01 的归一化容差，吸收四舍五入误差）。这条规则阻止模型「拿一个
真实的元素编号，塞一个完全不同的坐标」。

## 五、安全边界

- 模型无法创建观察记录，也无法指定编号。编号只能来自上一次真实观察的输出。
- 模型伪造的 `observation_id` 会得到 `observation_not_found`。
- 跨观察引用元素编号会得到 `element_not_found`。
- 整桌面观察会登记，但 `operable=False`，不能用于定位点击。
- 进程 DPI 感知不是 `per_monitor` 时，记录仍然保存，但 `operable=False`。
- 校验只读窗口元数据和屏幕度量，不截图、不调用模型、不发送输入。

## 六、点击工具的约束

`click_element` 已经接入这套记录，但**只在模拟后端下验证，没有真实输入**：

1. 保持 `requires_confirmation = True`，继续走
   `prepare -> 用户确认 -> 服务端注入 -> execute`。
2. `retry_safe = False`。工作流引擎已经会检查这个标记：
   即使错误文本看起来像「超时 / 网络」，也不会自动再点一次。
3. 只接受 `observation_id + element_id`，由服务端根据记录计算坐标；
   模型自己塞坐标会直接被参数校验拒绝。
4. 执行前至少做两次观察记录校验（聚焦前、聚焦后），再核对前台窗口；
   任何一次失败都停止，不会自动重新观察后接着点——重新观察也必须
   重新获得用户确认。
5. 生产登记的工具默认没有输入后端，在 `prepare()` 阶段就失败关闭，
   不会弹确认卡片，也不会退回真实输入。

完整链路和失败情况见 [click.md](click.md)。

## 七、测试

```
.venv\Scripts\python.exe scripts\test_observation.py
```

全部使用假窗口、假屏幕度量、假时钟，离线运行，不产生任何真实桌面输入。
`scripts/test_vision_tool.py` 另外验证 `inspect_screen` 成功后会登记记录，
以及登记失败时工具会如实失败、不会把结果当成可信观察。
