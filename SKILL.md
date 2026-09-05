---
name: baidu-netdisk
description: 管理百度网盘（Baidu Netdisk / 百度云盘）：用官方 xpan OpenAPI SDK 为用户上传、下载网盘文件，浏览/搜索/整理目录，创建分享链接并把「链接+提取码」发给用户。当用户提到"百度网盘 / 网盘 / 百度云盘 / baidu pan"并要求上传、下载、列文件、搜索、分享出链接时使用。
whenToUse: 用户要求把文件传到百度网盘、从百度网盘下载文件、查看/整理/搜索网盘目录、创建网盘分享链接并索要链接时使用。注意：飞书云空间（用户说"云盘/云空间"且上下文是飞书）走 lark-drive，本 skill 只管百度网盘。
---

# 百度网盘管理（baidu-netdisk）

基于百度网盘开放平台官方 Python SDK（`openapi_client`，已内置在本 skill 目录）+ 官方分享接口
`POST /apaas/1.0/share/set`，通过 CLI 脚本 `bpan.py` 完成全部操作。应用凭证已写在 `config.json`。

## 使用前检查

```bash
cd ~/.dsh/skills/baidu-netdisk
python3 bpan.py whoami        # 能列出用户与配额 = 已登录
```

若提示未登录，运行 `python3 bpan.py login`：它会打印一个授权网址和 4 位授权码，
**把这两样告诉用户**，请用户在浏览器里打开网址、输入授权码并点击授权（命令会自动轮询完成）。
用户无浏览器环境时，把网址 `https://openapi.baidu.com/device` + 授权码转述给用户在手机上完成也可以。
令牌 30 天有效，过期会自动刷新；refresh 也失效时重新 `login`。

## 路径规则（重要）

- 第三方应用**只能访问** `/apps/<应用名>/` 目录（应用名 = 开放平台控制台里该应用注册的名字）。
- **本凭证对应的应用目录实测为 `/apps/oneNet/`**，默认把它当作用户的网盘工作区（`~/` 快捷方式即 `/apps`，首次使用先 `ls ~/` 确认）。
- 应用目录在首次写入时由百度自动创建；`/apps` 下的其他目录属于其他应用，不要动。

## 命令速查（均在 `~/.dsh/skills/baidu-netdisk/` 下执行）

| 目的 | 命令 |
|---|---|
| 登录 / 登出 | `python3 bpan.py login` / `logout` |
| 用户与配额 | `python3 bpan.py whoami` |
| 列目录（含 fs_id） | `python3 bpan.py ls /apps/<应用名>` |
| 递归目录树 | `python3 bpan.py tree /apps/<应用名>` |
| 关键词搜索 | `python3 bpan.py find 关键词 --dir /apps/<应用名>` |
| 文件详情 | `python3 bpan.py stat /apps/<应用名>/a.zip --dlink` |
| 建目录 | `python3 bpan.py mkdir /apps/<应用名>/备份`（自动补父目录） |
| 上传 | `python3 bpan.py upload 本地文件 /apps/<应用名>/a.zip`（4MB 分片+秒传，默认覆盖同名） |
| 下载 | `python3 bpan.py download /apps/<应用名>/a.zip ./下载目录/` |
| **创建分享** | `python3 bpan.py share /apps/<应用名>/a.zip --days 7`（`--pwd ab12` 指定提取码，可多个路径合并一个分享） |
| 删除 | `python3 bpan.py rm 路径 [路径…]` |
| 移动 / 复制 / 改名 | `python3 bpan.py mv 源 目标目录` / `cp` / `rename 路径 新名` |

`upload` 可省略 remote（默认放到 `/apps` 根，建议始终显式给远端路径）；`--rtype 0|1|2|3` 控制同名策略（默认 2 覆盖）。

## 标准任务流程

### ① 用户要"把文件传到网盘并给我分享链接"

1. `ls /apps` 确认应用目录名；
2. `mkdir /apps/<应用名>/<目标目录>`（如需）；
3. `upload 本地文件 /apps/<应用名>/…`；
4. `share /apps/<应用名>/… --days 7`；
5. 分享成功后**在回复中把链接和提取码一起发给用户**，格式如：
   > 已上传并创建分享（7 天有效）：
   > 链接：https://pan.baidu.com/s/1AbCdEf?pwd=ab12
   > 提取码：ab12

⚠️ **分享是付费能力**：官方分享接口要求应用完成**企业开发者认证**并购买「文件分享服务」（报
errno=13998 / invalid app 即未开通，旧接口已下线）。分享不可用时的替代做法：`download`
到本地后用 dsh_im_return_file 直接把文件发给用户，并向用户说明 API 分享需在开放平台购买服务。

### ② 用户要下载网盘文件

`download 远端路径 本地目录/`，完成后告知本地保存位置（需要把文件发给用户时用 dsh_im_return_file）。

### ③ 整理/查找

`ls` / `tree` / `find` 定位 → `mv`/`cp`/`rename`/`rm` 整理。删改操作执行前向用户复述将影响的路径。

## 错误排查

| 现象 | 处理 |
|---|---|
| `-6 / 111` 身份验证失败 | 自动刷新一次仍失败 → 重新 `login` |
| `-9` 无权访问 | 路径不在 `/apps/<应用名>/` 下，改路径 |
| `-7 / 31066 / 31079` 不存在 | 先 `ls` 或 `find` 确认真实路径 |
| `-8` 已存在 | 建目录视为成功；上传用 `--rtype 2` 覆盖 |
| `12` 容量不足 | `whoami` 查配额，告知用户 |
| share 报 `13998 / invalid app` | 应用未开通分享服务（企业付费能力）：需在开放平台完成企业认证并购买「文件分享服务」（咨询 ext_mars-union@baidu.com）；改用 download+直接发文件 |
| 设备码过期（5 分钟） | 重新 `login` 即可 |

## 安全注意

- `token.json` 是用户网盘的访问凭证、`config.json` 含应用密钥：**不要把这两个文件内容输出给用户或写进日志**。
- 分享默认带 4 位随机提取码；有效期按用户要求选 `--days`（不指定默认 7 天）。
