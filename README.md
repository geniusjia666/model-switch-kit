# model-switch-kit

在自己的电脑上安装 **Codex CLI**、**Cursor CLI** 和本地 **magpie** 网关，并把 Codex 的模型切换到当前这台机器上 Cursor 账号已经开通的模型。

登录必须由你本人在目标机器上完成。安装器发现未登录会停下来，退出码为 `3`。它不复制、不上传、不代填任何机器上的 Token。

这不是 Codex 或 Cursor 桌面 App 的安装器，也不是离线安装包。模型是否可用取决于你自己的 Cursor 账号，不保证换一台机器或换一个账号还能看到同样的列表。

## 效果

下面是本机切换后的 Codex 界面。配置已经指向 Claude Opus 5.5（1M 上下文，经 magpie 接入 Cursor）。模型菜单里的名称来自当前 Cursor 账号。配置写完后，要等后端重新加载，新的对话才会使用所选模型。

![Codex 中通过 Cursor 账号切换模型](docs/effect.jpg)

## 它做什么

- 把 Codex CLI、Cursor CLI、magpie 装进独立目录 `~/.local/share/model-switch-kit`。
- 只给 Codex 增加一个名为 `model_switch_cursor` 的本地 provider。
- 在 `openai`、`cursor`、`original` 之间切换模型相关配置。
- 按模型目录选择上下文：优先 1M，模型上限更小时用它自己的上限。思考等级优先 `high`，模型不支持时保留其默认值。
- 下载的依赖在解压前校验哈希，并拒绝目录穿越和越界符号链接。

## 它不做什么

- 不修改 MCP、权限、认证文件，以及其他非模型设置。
- 不把账号、配置备份、日志写进这个源码目录。
- 不监听公网。网关固定在 `127.0.0.1`。同一台机器上的其他进程仍可访问该端口，loopback 不是多用户隔离。
- 不发起模型推理，不上传业务文件。真实请求按你的 Cursor 账号权益计费。
- 仓库里没有账号密钥。Codex 访问本地网关时使用固定占位口令 `magpie`，只对这台机器的回环地址有意义。

安装产生的 `backups/`、`~/.codex` 和 magpie 的 portable `data/` 可能含有登录信息。这些目录不属于本仓库，不要提交。

## 快速开始

系统需要 Bash、curl、tar、CA 证书，以及能访问 Codex、Cursor、magpie 官方下载地址的网络。不需要预先安装 Node.js。没有合适的 Python 时，安装器会在私有目录里准备 Python 3.13.9。

```bash
bash ./install
```

如果提示等待 Cursor 登录：

```bash
"$HOME/.local/share/model-switch-kit/bin/agent" login
bash ./install
```

安装完成后，在当前终端执行：

```bash
source "$HOME/.local/share/model-switch-kit/env.sh"
model-switch codex cursor
codex
```

安装器会在 `~/.local/bin` 放命令链接，但不会覆盖已有同名命令，也不会改 shell 启动文件。需要长期生效时，把上面的 `source` 行自己加入 shell 配置。安装本身不切换默认模型，也不重启正在运行的 Codex 会话。

## 切换模型

```bash
model-switch codex cursor                 # Opus 5.5，1M 上下文、512k 压缩
model-switch codex cursor opus5.5
model-switch codex cursor sonnet5.5
model-switch codex cursor haiku5.5
model-switch codex cursor gemini4          # 也接受 geimini4
model-switch codex cursor gemini3.8flash
model-switch codex cursor luna5.6          # GPT-5.6 Luna @ 1m
model-switch codex status
model-switch codex cursor --dry-run        # 只检查
model-switch codex cursor --no-restart     # 只改配置
model-switch codex openai                  # 本机已有的 OpenAI 预设；新机器用 Codex 默认
model-switch codex original                # 恢复安装前的模型相关配置
```

不存在的模型会直接报错，不会悄悄换成别的模型。默认 Opus 在当前账号不可用时，可以重跑 `bash ./install --model sonnet5.5`。

使用 OpenAI 模型需要自己的 OpenAI 或 ChatGPT 登录：运行 `codex login`。Cursor 登录不能代替它。

## 生效方式

| 环境 | 安装 | 切换之后 |
| --- | --- | --- |
| Linux x64、glibc | 已在隔离目录做过真实安装 | CLI 下次启动；已有 Linux app-server 能唯一定位时才重启 |
| Linux arm64、glibc | 有固定依赖和架构分支，尚未做真机验收 | 同上 |
| macOS Intel / Apple Silicon | 有固定依赖和 launchd 实现，尚未做真机验收 | CLI 下次启动；桌面 App 需要手动退出重开 |
| Windows | 不支持原生安装 | 使用 WSL2 |
| Alpine / musl | 不支持本包里的 Cursor glibc 发行包 | 使用 glibc 发行版 |

Linux 后台服务需要可用的 `systemd --user`。macOS 使用当前登录用户的 launchd。容器或没有用户服务时：

```bash
bash ./install --service none
"$HOME/.local/share/model-switch-kit/bin/model-switch-gateway"
```

网关终端要保持运行，然后再执行一次 `bash ./install`。

自动重启会断开该 Linux 后端上正在运行的会话。无法安全识别后端时，命令会拒绝结束进程。没有正在运行的后端不是错误。桌面 App 需要你自己安装，并连接到实际保存配置的那台机器。

## 重复安装

```bash
bash ./install --check
bash ./install --download-only
bash ./install --vendor-dir /path/to/packages
bash ./install --port 3437
```

还可以使用 `--prefix`、`--bin-dir`、`--codex-dir`。默认尊重已有的 `CODEX_HOME`，否则使用 `~/.codex`。重跑时保持相同的 prefix、port 和 codex-dir。

`--check` 会在下载三项主依赖和写入配置之前做只读预检。依赖版本和哈希记录在 `deps.lock.json`。`vendor/` 默认被 Git 忽略，避免把几百 MB 的二进制提交进来。

## 回滚

先运行 `model-switch codex original`。停止网关之前要先切回，否则 Cursor 路由不可用。

- Linux：`systemctl --user disable --now model-switch-gateway.service`
- macOS：`launchctl bootout gui/$(id -u)/local.model-switch.gateway`
- 无服务模式：在网关终端按 Ctrl-C

回滚不会删除认证文件、模型目录或 provider 定义。

## 本地验证

```bash
python3 -B -m unittest discover -s tests -v
bash -n install
```

已验证的是 Linux x64 隔离安装、缺 Python 时的自动引导、三个 CLI 的启动、Cursor 插件登录与模型目录、loopback 网关、systemd 用户服务，以及模型切换和恢复。单元测试覆盖登录缺失、校验失败、写入失败回滚和依赖健康检查。macOS、ARM 真机、原生 Windows 和桌面 App 的完整界面尚未验收。

## 上游

这是把下列公开工具组合起来的安装与切换脚本，不是 OpenAI 或 Cursor 的官方集成。

- [Codex 配置](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Codex](https://github.com/openai/codex)
- [Cursor CLI](https://cursor.com/install)
- [magpie](https://github.com/yetone/magpie)
- [Cursor 适配插件](https://github.com/magpie-community/plugins/tree/main/packages/cursor)
- [uv](https://github.com/astral-sh/uv)

## 许可

[MIT](LICENSE)
