# dsh-netdisk

用 AI 管理百度网盘的技能（Skill）：基于百度网盘开放平台**官方 xpan Python SDK** + 官方分享接口，
把「上传 / 下载 / 浏览 / 搜索 / 整理 / 分享」封装成一个 CLI（`bpan.py`），供 AI Agent（如
DeepSeek Harness / DSH、Claude Code 等）在对话中直接驱动，也可以当普通命令行工具人工使用。

## 功能

- 🔐 设备码授权登录（浏览器输一次授权码，令牌 30 天有效、自动刷新）
- 📤 上传：4MB 分片 + MD5 秒传，自动补齐父目录，同名可覆盖/改名/报错（`--rtype`）
- 📥 下载：官方 dlink 直链，流式写入，带进度
- 📂 浏览：`ls` / `tree` / `find` 关键词搜索 / `stat` 文件详情
- 🗂 管理：`mkdir` / `rm` / `mv` / `cp` / `rename`
- 🔗 分享：创建分享链接 + 提取码（⚠️ 受百度付费能力限制，见下文）
- 🤖 所有命令支持 `--json` 输出，方便 AI 解析

## 安装

### 一键安装（推荐）

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/toddpan/dsh-netdisk/main/install.sh)
```

安装脚本会自动：检测 Python 环境 → 装到 `~/.dsh/skills/baidu-netdisk`（无 `~/.dsh` 时装到
`~/dsh-netdisk`）→ 安装依赖 → 启动**密钥配置向导**（支持把控制台凭证区域整段复制粘贴，
自动解析 AppID/AppKey/SecretKey/SignKey）→ 询问是否立即进行百度账号授权登录。

常用参数与环境变量：

```bash
# 非交互安装（CI / AI 代理），凭证走环境变量
BPAN_APPID=124250552 BPAN_APPKEY=xxx BPAN_SECRETKEY=xxx \
  bash <(curl -fsSL https://raw.githubusercontent.com/toddpan/dsh-netdisk/main/install.sh) -y

# 重新运行密钥向导 / 指定安装目录 / 装完直接登录
curl -fsSL https://raw.githubusercontent.com/toddpan/dsh-netdisk/main/install.sh -o install.sh
bash install.sh --reconfig
bash install.sh --dir /path/to/dir
bash install.sh --login

# 应答文件批量安装（首行 1=粘贴模式 2=逐项，随后按行给值，空行结束）
printf '1\nAppID: ...\nAppKey: ...\nSecretKey: ...\n\n' \
  | BPAN_WIZARD_STDIN=1 bash install.sh --reconfig --no-login
```

重复运行 = 自动更新（git pull），已有 config.json 不会被覆盖。

### 手动安装

```bash
# 1. 克隆到 DSH 技能目录（其他 Agent 平台放到对应 skills 目录即可）
git clone https://github.com/toddpan/dsh-netdisk.git ~/.dsh/skills/baidu-netdisk

# 2. 安装依赖（Python 3.9+）
python3 -m pip install requests "urllib3>=1.25.3"

# 3. 填写应用凭证
cp config.example.json config.json
vi config.json   # 填入百度网盘开放平台的应用凭证
```

凭证在[百度网盘开放平台控制台](https://pan.baidu.com/union/console)创建应用后获取
（AppID / AppKey / SecretKey / SignKey）。

## 登录

```bash
cd ~/.dsh/skills/baidu-netdisk
python3 bpan.py login
```

按提示用浏览器打开 `https://openapi.baidu.com/device`，登录百度账号并输入授权码。
授权码 5 分钟有效，过期会**自动换新码**继续等待。令牌保存在 `token.json`（已在 `.gitignore` 中）。

## 命令速查

```bash
python3 bpan.py whoami                          # 用户与配额
python3 bpan.py ls /apps/<应用名>                # 列目录（含 fs_id）
python3 bpan.py tree /apps/<应用名>              # 递归目录树
python3 bpan.py find 关键词 --dir /apps/<应用名>  # 搜索
python3 bpan.py mkdir /apps/<应用名>/备份         # 建目录（自动补父目录）
python3 bpan.py upload ./a.zip /apps/<应用名>/a.zip   # 上传（秒传+分片）
python3 bpan.py download /apps/<应用名>/a.zip ./out/  # 下载
python3 bpan.py share /apps/<应用名>/a.zip --days 7   # 创建分享链接
python3 bpan.py rm / mv / cp / rename            # 删除/移动/复制/改名
```

> 第三方应用只能访问 `/apps/<应用名>/` 目录（应用名 = 控制台里应用注册名），
> 该目录在应用首次写入时由百度自动创建。

## 关于分享能力（重要）

官方 API 创建分享链接（`POST /apaas/1.0/share/set`）是**企业开发者付费能力**：

- 需完成企业实名认证并购买「文件分享服务」（咨询 `ext_mars-union@baidu.com`）
- 未开通的应用调用返回 `errno=13998 invalid app`（本仓库实测）
- 旧版 `xpan/share?method=rapidshare` 接口已下线（实测返回空 body）

未开通分享服务时，`share` 命令会明确报错并给出替代方案（下载到本地后直接发送文件）；
开通后本命令无需任何改动即可返回「链接 + 提取码」。

## 实测踩坑记录

对接官方 API 时验证过的事实，供后来者少走弯路：

| 坑 | 结论 |
|---|---|
| 分片上传域名 | `superfile2` 必须走 `d.pcs.baidu.com`，`pan.baidu.com` 会 403 |
| superfile2 响应头 | 误标 `Content-Encoding: gzip`，requests 解码会崩，需 `Accept-Encoding: identity` |
| precreate 响应 block_list | 是**分片下标数组**（如 `[0,2]`），不是 MD5 列表 |
| `xpan/file?method=create` | 参数必须放 **POST body**（form），放 query 报 `errno=2` |
| `filemetas` 的 fsids | 必须是**整数**数组；分享接口 `fsid_list` 则要求**字符串**数组 |
| `search` 接口 | 传 `page=0` 会报 `errno=2`，去掉即可 |
| `listall` 接口 | 不允许 `/apps` 下路径（`errno=20020`），递归列表需用 `method=list` 逐层遍历 |
| `xpanfilelist` 分页 | 参数是 `dir/start/limit`（不是 `parent_path/page/num`，那是 doclist 的） |

## 安全说明

- `config.json`（应用密钥）与 `token.json`（网盘访问令牌）已在 `.gitignore` 中，**不会也不会**提交到仓库
- 分享默认带 4 位随机提取码，有效期按需指定（`--days`，默认 7 天）
- 下载直链 dlink 含签名、8 小时有效，且必须携带 access_token 才能访问，不要外泄令牌

## 相关链接

- [百度网盘开放平台文档](https://pan.baidu.com/union/doc)
- [Python SDK（本仓库 openapi_client 即官方 2022.06.16 版）](https://pan.baidu.com/union/doc)
