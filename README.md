# DSH Desktop Attach（实验版）

在 iPhone Safari 连接 Mac 上正在运行的 DeepSeek Harness（DSH）桌面会话，可添加到主屏幕。手机作为同一个桌面后端的另一浏览器客户端，使用原生对话、附件、审批、模型与工具能力。

这是独立的本地适配工具，与 DeepSeek 无隶属或背书关系。它依赖 DSH 的内部认证格式和前端结构，桌面升级后可能失效。

## 工作方式

`attach.py` 发现桌面 profile 的本机监听端口，转发 HTTP 和 WebSocket，并在本机使用 DSH 已有的浏览器认证材料。它不另外启动第二个 DSH Host。`mobile.css` 和 `mobile.js` 只为原生界面添加手机布局、导航及键盘适配。

## 安装

要求 macOS、可工作的 Python 3.9+，以及已经可以正常使用的 DSH 桌面应用。仅使用 Python 标准库，不需要 pip 依赖或 sudo。

```bash
git clone https://github.com/oceanliangcn716/dsh-desktop-attach.git ~/.local/share/dsh-desktop-attach
cd ~/.local/share/dsh-desktop-attach
./install.sh
```

打开 DSH 桌面应用，双击桌面的 **DSH手机连接**。在 iPhone Safari 粘贴链接打开，再选择“分享 → 添加到主屏幕”。在外时先通过 Tailscale 或自己的 VPN 回到局域网。

如果原主屏幕入口需要重新配对，把新复制的配对链接粘贴到该页面即可。

## 配置与维护

首次安装从 `config.example.json` 创建 `config.json`，保留已有配置。

- `bind_host`：默认 `auto`，启动时自动选择局域网地址。
- `port`：默认 `3081`。
- `mobile_ui`：默认 `true`，注入手机布局。

```bash
# 仅准备文件和连接工具，不注册或启动服务
INSTALL_NO_START=1 ./install.sh
# 手动启动
python3 attach.py serve
# 复制配对链接，不在终端打印密钥
python3 attach.py url --copy
# 查看状态，仅显示是否已配置授权和发现的桌面端口
python3 attach.py status
# 重启安装器注册的服务
launchctl kickstart -k gui/$(id -u)/local.$(id -u).dsh-desktop-attach
# 移除本安装的登录服务和快捷方式，保留程序及数据
./uninstall.sh
```

`url` 不加 `--copy` 会在终端显示配对链接，请谨慎使用。IP 改变后需要重启并重新复制链接。已有旧服务占用端口时先停止旧服务，安装器不会清理其他服务。

手机提示桌面 Host 不可用时，先打开 DSH 桌面应用。浏览器会话签名格式或桌面 profile 位置改变时，需要适配这个工具。

## 隐私与安全

发布包不包含任何用户的配置、DSH 凭据、配对密钥、对话、附件、截图、日志、模型文件或网络调试记录。主屏幕图标是在安装时生成的通用手机图案，不是厂商标志。

认证材料只从本机 DSH 官方存储中读取，不内置账号、令牌或项目方服务。转发的请求仍使用用户本机 DSH 配置的模型服务商，模型调用和文件读取适用其权限与计费。不要复制凭据到源码、问题反馈或发布附件。

配对链接具有控制权限，只给自己的设备。本版本授权保存在 HttpOnly Cookie；删除本目录 `gate.secret` 并重启可作废旧授权。HTTP 本身没有 TLS，请仅在可信局域网或加密 VPN 内使用，不要映射到公网。

## 开发与发布

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m py_compile attach.py
# Node.js 24 仅用于开发时的发布检查，运行 DSH 入口不需要 Node
git add <明确要发布的源文件>
node tools/check-release.mjs
```

离线测试使用合成请求及临时目录，不读取 DSH 真实凭据，不连接模型。尚未在另一台用户 Mac 或 Intel 真机上完成端到端安装验证。

发布 ZIP 从 Git 提交导出，不打包本机运行目录。许可证：[MIT](LICENSE)。DSH/DeepSeek 商标与应用适用各自权利及条款，本仓库不分发 DSH 官方程序或原生前端资源。
