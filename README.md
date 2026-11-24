# Github Copilot Proxy

使用 **Python** 实现的 GitHub Copilot 本地网关，提供 OpenAI Chat Completions 兼容接口。桌面界面使用 **PySide6 / Qt**，支持 Windows、macOS 和 Linux；Linux 服务器可只安装 CLI，不需要桌面、Node.js 或 .NET。

> 非 GitHub 官方项目。需要自己的有效 Copilot 订阅，并遵守 GitHub、模型供应商及组织的使用政策。代理连通不代表可以使用任何地区或账号未获授权的模型。

## 快速开始

### 1. 让 AI Agent 帮你安装

复制这一句话：

```text
请按 README 帮我安装并启动 https://github.com/jiangweifang/Github-Copilot-Proxy，先确认是桌面还是无界面服务器，需要 GitHub 浏览器授权时提醒我，完成后验证服务并告诉我访问地址。
```

### 2. 或者执行安装向导

需要 **Python 3.11+**（支持 Conda）和 Git；安装器使用项目独立 `.venv`，不修改 Conda 基础环境或系统 Python。

先克隆（已有仓库则跳过）：

```text
git clone https://github.com/jiangweifang/Github-Copilot-Proxy.git
```

进入项目目录：

| 场景 | 安装入口 |
| --- | --- |
| Windows 桌面 | 双击 `install-startup.bat` |
| macOS / Linux 桌面 | `sh install.sh` |
| Linux 服务器 / SSH | `sh install.sh --headless` |
| 已激活 Conda 或指定解释器 | `python bootstrap.py`，服务器加 `--headless` |

Windows 向导会检查常见 Conda 位置；找不到时可运行：

```powershell
.\install-startup.ps1 -Python "C:\path\to\python.exe"
```

桌面默认中文，右上角可在 **简体中文 / English** 之间切换，选择会自动保存。安装完成后，点击 **完成设置 / 启用登录自启动**：本人完成 GitHub 设备授权，然后注册当前用户登录自启动并启动接口。需要代理时先打开 **代理设置**。无界面模式会在终端显示授权地址与设备码，可在另一台有浏览器的设备完成授权。

依赖版本由 `constraints.txt` 固定。公司网络需要镜像时，请在安装前配置 `PIP_INDEX_URL`，不要提交带凭据的源地址。例如当前环境可使用：

```powershell
$env:PIP_INDEX_URL = 'https://packagefeedproxy.microsoft.io/pypi/simple/'
```

**从旧 Node 版本升级：先退出旧托盘和服务。** 新版不会同时启动另一套占用相同端口的服务，也不会为了迁移强杀未知进程。原安装目录内的本工具用户设置保持隔离，不读取其他 Copilot 工具共享的登录缓存。

### 3. 配置 WorkBuddy

| 配置项 | 默认值 |
| --- | --- |
| 协议 | OpenAI / Chat Completions |
| Base URL | `http://127.0.0.1:4140/v1` |
| 完整请求地址（若需要） | `http://127.0.0.1:4140/v1/chat/completions` |
| API Key | 留空；客户端不允许留空时填 `unused` |
| 模型 ID | 从 `http://127.0.0.1:4140/v1/models` 返回的目录选择 |

模型目录自动获取，不需要手写账号模型列表。不会自动改写 WorkBuddy 的配置。

## 桌面功能

- **语言**：默认简体中文，可即时切换 English；主窗口、已打开的代理设置、托盘和确认框同步更新。语言单独保存在用户目录的 `ui.json`，不会改动代理配置或 API 输出。
- **Dashboard**：状态、访问地址；服务停止时仅显示“启动服务”，运行时仅显示“停止服务”，停止前必须确认。其后按实际配置显示“开机自动启动”或“禁用自启动”，禁用前必须确认；这两个操作不影响当前服务，仅控制当前用户下次登录后的自动启动。
- **登录 / 退出登录（Sign in / Sign out）**：独立设备授权；登录成功后隐藏登录按钮，只显示用户名（获取失败时显示“登录成功”）和退出登录入口，退出后再显示登录按钮。退出登录必须确认（界面默认选择“否”，CLI 默认取消），确认后停止业务接口并清除登录，保留代理设置和自启动。再次登录系统时只显示未登录，不自动弹授权。
- **Network settings**：直连、HTTP（包括 HTTPS CONNECT）、SOCKS5，支持用户名/密码与代理端 DNS。
- **Test connection**：不保存设置，不消耗模型推理额度；未登录时测 GitHub 连通，登录后测 Copilot 权限和模型目录。
- **Save and apply**：立即切换上游连接，不重启进程；正在进行的请求可能中断，会明确返回失败，不自动重发聊天。
- **托盘**：支持的桌面环境中显示 Github Copilot Proxy，可打开主窗口、设置代理或退出。部分 Linux 桌面需要 AppIndicator/StatusNotifier 支持；无托盘时仍可使用主窗口。

代理密码按用户选择**明文保存在私有用户配置文件**，输入框掩码显示，状态与日志不回显。请勿分享整个配置目录。

## CLI（不需要 Qt）

安装后可使用 `copilot-proxy`，或指定虚拟环境解释器：

```text
python -m copilot_proxy.cli serve --root <项目目录>
python -m copilot_proxy.cli login --root <项目目录>
python -m copilot_proxy.cli status --root <项目目录>
python -m copilot_proxy.cli stop --root <项目目录>
python -m copilot_proxy.cli start --root <项目目录>
python -m copilot_proxy.cli logout --root <项目目录>
python -m copilot_proxy.cli shutdown --root <项目目录>
python -m copilot_proxy.cli models --root <项目目录>
```

`login` 登录完成后退出，不会变成常驻服务。`serve` 才启动常驻管理进程；未登录时不监听业务端口。`Ctrl+C` 停止前台服务。管理命令通过仅监听回环地址、带随机认证口令的私有控制通道通信，不暴露到业务 HTTP 接口。

`proxy-set` / `proxy-test` 从标准输入读取 JSON，避免把密码放入进程命令行：

```json
{"type":"socks5","host":"127.0.0.1","port":1080,"username":"","password":""}
```

桌面再次启动可运行 `.\start.ps1`（Windows）或 `sh start.sh`（macOS/Linux）；服务器用 `sh start.sh --headless`。

## 监听配置与接口

项目的 `adapter-config.json` 保持兼容：

```json
{
  "host": "127.0.0.1",
  "port": 4140,
  "bridgePort": 4141
}
```

- `host`：合法 IPv4；`127.0.0.1` 仅本机，`0.0.0.0` 监听所有 IPv4 网卡，也可填写本机已有的具体地址。
- 两个端口须为 `1..65535` 内不同的整数。现在由同一 Python 进程监听，不再运行 Node 桥接进程。
- `port` 提供 `/v1/models`、`/v1/chat/completions`；`bridgePort` 为兼容端口，额外提供 `/v1/responses`。客户端应连接 `port`。
- 模型按需缓存 5 分钟；未知模型最多每 10 秒触发一次提前刷新。改变代理或退出登录会清除旧目录及路由缓存。
- 原始模型 API 返回的 `supported_endpoints` 用于路由；缺少声明且上游明确不支持 Chat Completions 时才自动尝试 Responses。
- 可选 `models` 数组仍可按 `id` 覆盖路由属性，但不能添加账号目录之外的模型。
- 支持文本、图片、函数工具及历史、推理和 token 用量转换，以及 SSE 流。Responses 路径仅支持 `n=1`，不支持 `stop`；请求体上限 16 MiB。
- 不校验本地 API Key 或浏览器 Origin；浏览器自身 CORS/本地网络策略仍然有效。

**不要向公网或不可信网络开放端口。** 能访问业务接口的程序可以消耗账号额度。应用不修改防火墙或系统代理；TLS 验证始终启用。填 `0.0.0.0` 后，其他设备应使用服务机器的实际 IP，而不是把 `0.0.0.0` 填成访问地址。

## 自启动与干净卸载

自启动仅作用于当前用户：

- Windows：当前用户 `Run` 注册表项；升级时移除属于同一项目的旧快捷方式。
- macOS：用户 `LaunchAgents`，下次登录生效。
- Linux 桌面：XDG `autostart`。
- Linux 无界面：systemd 用户服务，需要可用的用户会话；不自动开启需要管理员授权的 linger。

退出程序不会退出 GitHub 登录；**Sign out** 才删除本工具的登录。删除本地凭据不等于在 GitHub 网站撤销 OAuth 授权。

卸载可用桌面的 **卸载（Uninstall）**、Windows 的 `uninstall.bat`、`sh uninstall.sh` 或 CLI `uninstall`。桌面先弹出清理范围确认框，默认选择“否”；取消不停止服务、不删除数据。确认后才停止本工具，移除本安装的自启动、用户配置、登录、代理密码、缓存、已知日志和旧 `node_modules`；权限或文件占用错误会明确报告，可解决后重试。

**最后退出程序，手动删除整个项目目录，包括 `.venv`、源码和 `.git`。** 运行中的解释器不能可靠地删除自身，因此不会把这一步伪装为已完成。不清除系统/Conda Python、全局 pip 缓存或其他工具的数据。

用户数据按安装目录隔离，位于：

- Windows：`%LOCALAPPDATA%\GithubCopilotProxy\<目录哈希>`
- macOS：`~/Library/Application Support/GithubCopilotProxy/<目录哈希>`
- Linux：`${XDG_CONFIG_HOME:-~/.config}/GithubCopilotProxy/<目录哈希>`

## 开发与测试

```text
python -m pip install -c constraints.txt -e ".[desktop]"
python -m unittest discover -s tests -v
```

仅安装核心用 `-e .`。测试不使用真实账号、不调用计费模型；覆盖协议转换、HTTP/SOCKS5 认证传输、代理切换、接口流式错误、登录取消、控制鉴权和归属清理。Qt 使用离屏测试，专门验证设置对话框与主窗口状态刷新不会互相覆盖。

桌面测试需要 `desktop` 可选依赖；服务器环境会跳过 Qt 测试。Linux 桌面还需对应发行版的 Qt/X11/Wayland 运行库；无界面部署不需要这些组件。

## 排错

| 现象 | 处理 |
| --- | --- |
| 找不到 Python | 激活 Python 3.11+ 的 Conda 环境，或为安装脚本指定解释器绝对路径 |
| 端口已占用 / 旧 Node 控制记录存在 | 先退出旧版本，勿并行启动两个版本 |
| 无法访问模型目录 | 查看 Dashboard 状态，完成登录并测试代理 |
| `401` / `403` | 检查账号、订阅和组织授权；代理不能改变服务权限 |
| 无托盘图标 | 展开隐藏图标，或检查 Linux AppIndicator 支持；主窗口仍可使用 |
| 模型仅支持 Responses | 通常按目录声明自动选择；必要时使用 `models` 路由覆盖 |
| 卸载报权限/占用错误 | 先关闭相关窗口和本项目进程，再重试；不要删除其他工具的凭据 |
