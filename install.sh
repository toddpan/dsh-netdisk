#!/usr/bin/env bash
# ============================================================================
# dsh-netdisk 一键安装脚本
#
#   一键安装（在线）：
#     bash <(curl -fsSL https://raw.githubusercontent.com/toddpan/dsh-netdisk/main/install.sh)
#
#   带参数：
#     curl -fsSL .../install.sh -o install.sh && bash install.sh [--dir 路径] [--reconfig] [--login] [-y]
#
#   非交互（CI / AI 代理）：
#     BPAN_APPID=xxx BPAN_APPKEY=xxx BPAN_SECRETKEY=xxx bash install.sh -y
#
# 功能：装依赖 → 装/更新 skill → 密钥配置向导（支持直接粘贴控制台凭证文本）→ 可选登录
# 兼容 macOS 自带 bash 3.2；管道执行时交互输入走 /dev/tty。
# ============================================================================
set -euo pipefail

REPO_URL="https://github.com/toddpan/dsh-netdisk.git"
RAW_BASE="https://raw.githubusercontent.com/toddpan/dsh-netdisk/main"
TARBALL="https://github.com/toddpan/dsh-netdisk/archive/refs/heads/main.tar.gz"
BRANCH="main"

ASSUME_YES=0; RECONFIG=0; WANT_LOGIN=0; NO_LOGIN=0
BPAN_INSTALL_DIR="${BPAN_INSTALL_DIR:-}"

# ---------------------------------------------------------------- 输出工具
if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
  C_G=$'\033[32m'; C_Y=$'\033[33m'; C_R=$'\033[31m'; C_B=$'\033[36m'; C_0=$'\033[0m'
else
  C_G=""; C_Y=""; C_R=""; C_B=""; C_0=""
fi
info()  { printf '%s\n' "${C_G}==> ${C_0}$*"; }
warn()  { printf '%s\n' "${C_Y}⚠️  ${C_0}$*"; }
die()   { printf '%s\n' "${C_R}❌ $*${C_0}" >&2; exit 1; }

usage() {
  cat <<'USAGE'
dsh-netdisk 一键安装脚本

用法:
  bash install.sh [选项]

选项:
  --dir <路径>    安装目录（默认: ~/.dsh/skills/baidu-netdisk，无 ~/.dsh 时为 ~/dsh-netdisk）
  --reconfig      强制重新运行密钥配置向导
  --login         安装后直接进入百度账号授权
  --no-login      安装后不询问授权
  -y, --yes       非交互模式：不询问，全部用默认值/环境变量
  -h, --help      帮助

环境变量:
  BPAN_INSTALL_DIR            安装目录
  BPAN_APPID / BPAN_APPKEY / BPAN_SECRETKEY / BPAN_SIGNKEY   应用凭证
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes)   ASSUME_YES=1 ;;
    --reconfig) RECONFIG=1 ;;
    --login)    WANT_LOGIN=1 ;;
    --no-login) NO_LOGIN=1 ;;
    --dir)      [ $# -ge 2 ] || die "--dir 需要一个路径参数"; BPAN_INSTALL_DIR="$2"; shift ;;
    -h|--help)  usage; exit 0 ;;
    *)          die "未知参数: $1（-h 查看帮助）" ;;
  esac
  shift
done

# ---------------------------------------------------------------- 应答预读
# BPAN_WIZARD_STDIN=1 时，先把外层 stdin 的应答完整存入临时文件，
# 避免中途的 git/pip 等子命令抢先消费管道内容。
ANSWERS_FILE=""
if [ "${BPAN_WIZARD_STDIN:-}" = "1" ]; then
  ANSWERS_FILE="$(mktemp "${TMPDIR:-/tmp}/bpan_answers.XXXXXX")"
  cat > "$ANSWERS_FILE"
fi

# ---------------------------------------------------------------- 0. 环境检查
command -v python3 >/dev/null 2>&1 || die "未找到 python3，请先安装 Python 3.9+"
info "检测到 $(python3 --version 2>&1)"

HAS_GIT=0
command -v git >/dev/null 2>&1 && HAS_GIT=1
[ "$HAS_GIT" = "1" ] || warn "未找到 git，将改用 tar 包下载（后续无法 git pull 更新）"

# ---------------------------------------------------------------- 1. 安装目录
if [ -z "$BPAN_INSTALL_DIR" ]; then
  if [ -d "$HOME/.dsh/skills" ]; then
    BPAN_INSTALL_DIR="$HOME/.dsh/skills/baidu-netdisk"
  else
    BPAN_INSTALL_DIR="$HOME/dsh-netdisk"
  fi
fi
info "安装目录: $BPAN_INSTALL_DIR"

fetch_fresh() { # $1 = 目标目录
  local dst="$1" tmp
  tmp="${dst%.tar}.download.$$"
  mkdir -p "$(dirname "$dst")"   # 目标父目录可能不存在（如首次创建 ~/.dsh/skills）
  if [ "$HAS_GIT" = "1" ]; then
    git clone --depth 1 -b "$BRANCH" "$REPO_URL" "$tmp" >/dev/null 2>&1 \
      || die "git clone 失败，请检查网络后重试"
  else
    mkdir -p "$tmp"
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL "$TARBALL" | tar xz -C "$tmp" --strip-components 1 \
        || die "下载源码包失败，请检查网络后重试"
    else
      wget -qO- "$TARBALL" | tar xz -C "$tmp" --strip-components 1 \
        || die "下载源码包失败，请检查网络后重试"
    fi
  fi
  mv "$tmp" "$dst"
}

if [ -d "$BPAN_INSTALL_DIR" ]; then
  if [ -d "$BPAN_INSTALL_DIR/.git" ]; then
    info "已存在，拉取更新…"
    if git -C "$BPAN_INSTALL_DIR" pull --ff-only origin "$BRANCH" >/dev/null 2>&1; then
      info "更新完成"
    else
      warn "git pull 失败（可能有本地改动或网络问题），保留现有版本继续安装流程"
    fi
  else
    backup="${BPAN_INSTALL_DIR}.bak.$(date +%Y%m%d%H%M%S)"
    warn "目录已存在且不是 git 仓库，备份到 $backup 后重新安装"
    mv "$BPAN_INSTALL_DIR" "$backup"
    fetch_fresh "$BPAN_INSTALL_DIR"
  fi
else
  info "下载 dsh-netdisk…"
  fetch_fresh "$BPAN_INSTALL_DIR"
fi

# ---------------------------------------------------------------- 2. Python 依赖
cd "$BPAN_INSTALL_DIR"
if python3 -c "import requests, urllib3, dateutil" >/dev/null 2>&1; then
  info "Python 依赖已就绪（requests / urllib3 / python-dateutil）"
else
  info "安装 Python 依赖…"
  if python3 -m pip install -q --user requests "urllib3>=1.25.3" python-dateutil >/dev/null 2>&1 \
     || python3 -m pip install -q --user --break-system-packages requests "urllib3>=1.25.3" python-dateutil >/dev/null 2>&1 \
     || python3 -m pip install -q --break-system-packages requests "urllib3>=1.25.3" python-dateutil >/dev/null 2>&1; then
    info "依赖安装完成"
  else
    warn "pip 自动安装失败，请手动执行: python3 -m pip install --user requests urllib3 python-dateutil"
  fi
fi

# ---------------------------------------------------------------- 3. 密钥配置向导
# 交互输入一律走 /dev/tty（兼容 curl | bash 管道执行）；环境变量可静默配置。
CFG="$BPAN_INSTALL_DIR/config.json"
WIZARD_MODE="auto"
[ "$RECONFIG" = "1" ] && WIZARD_MODE="force"

python3 - "$CFG" "$WIZARD_MODE" "$ANSWERS_FILE" <<'PYEOF'
import json, os, re, sys

cfg_path, mode = sys.argv[1], sys.argv[2]
FIELDS = [('app_id', 'AppID'), ('app_key', 'AppKey'), ('secret_key', 'SecretKey'),
          ('sign_key', 'SignKey'), ('share_secret', 'ShareSecret'),
          ('share_third_id', 'ShareThirdId')]

def read_config():
    try:
        with open(cfg_path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def complete(cfg):
    return bool(cfg.get('app_key') and cfg.get('secret_key'))

def mask(v):
    v = v or ''
    return (v[:4] + '****' + v[-2:]) if len(v) > 8 else ('已填' if v else '未填')

cfg = read_config()
if mode != 'force' and complete(cfg):
    print('==> 检测到已配置的 config.json（app_key=%s），跳过密钥向导' % mask(cfg.get('app_key')))
    print('    如需重新配置: bash install.sh --reconfig')
    sys.exit(0)

def env_get(k):
    # 同时接受 BPAN_APP_KEY 与 BPAN_APPKEY 两种拼写
    for a in ('BPAN_' + k.upper(), 'BPAN_' + k.upper().replace('_', '')):
        v = os.environ.get(a, '')
        if v.strip():
            return v.strip()
    return ''

env = {k: env_get(k) for k, _ in FIELDS}
if env.get('app_key') and env.get('secret_key'):
    merged = dict(cfg)
    for k, v in env.items():
        if v:
            merged[k] = v
    merged.setdefault('scope', 'basic,netdisk')
    merged.setdefault('remote_root', '/apps')
    with open(cfg_path, 'w', encoding='utf-8') as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    print('==> 已从环境变量写入配置（app_key=%s, secret_key=%s）' % (mask(env['app_key']), mask(env['secret_key'])))
    sys.exit(0)

def open_tty():
    """交互输入源：默认 /dev/tty（兼容 curl|bash 管道执行，stdin 被脚本占用）；
    BPAN_WIZARD_STDIN=1 时读 stdin（应答文件/批量安装）。
    提示文本走 stderr，即使 stdout 被重定向到文件也可见。"""
    if os.environ.get('BPAN_WIZARD_STDIN') == '1':
        ans = sys.argv[3] if len(sys.argv) > 3 else ''
        if ans and os.path.exists(ans):
            return open(ans, 'r', encoding='utf-8')
        return None
    try:
        f = open('/dev/tty', 'r')  # 只读打开：部分环境 /dev/tty 为管道，'r+' 会报不可定位
    except Exception:
        return None
    if not os.isatty(f.fileno()):
        f.close()
        return None
    return f

tty = open_tty()
if tty is None:
    print('==> 非交互环境，跳过密钥向导。稍后可任选其一完成配置：')
    print('    1) 重新运行: BPAN_APPID=… BPAN_APPKEY=… BPAN_SECRETKEY=… bash install.sh --reconfig')
    print('    2) 应答文件: BPAN_WIZARD_STDIN=1 bash install.sh --reconfig < answers.txt')
    print('       （answers.txt 首行填 1 或 2，随后粘贴/逐行输入凭证，空行结束）')
    print('    3) 手动编辑: %s（参考 config.example.json）' % cfg_path)
    sys.exit(0)

def ask(prompt, default=''):
    print('%s%s' % (prompt, (' [%s]' % default) if default else ''), file=sys.stderr)
    sys.stderr.flush()
    line = tty.readline()
    if not line:  # EOF
        return default
    v = line.rstrip('\n').strip()
    return v or default

def tty_print(s=''):
    print(s, file=sys.stderr)

tty_print()
tty_print('┌──────────────────────────────────────────────────┐')
tty_print('│           🔑 百度网盘开放平台 密钥配置向导          │')
tty_print('└──────────────────────────────────────────────────┘')
tty_print('凭证获取: https://pan.baidu.com/union/console 创建应用后可见')
tty_print('（AppID / AppKey / SecretKey / SignKey）')
if complete(cfg):
    tty_print('检测到已有配置（app_key=%s），继续将覆盖。' % mask(cfg.get('app_key')))

mode_in = ask('配置方式: [1] 粘贴控制台整段凭证文本  [2] 逐项输入  [3] 稍后再配 （回车=1）', '1')

if mode_in == '3':
    tty_print('已跳过。之后手动复制 config.example.json 为 config.json 填写即可。')
    sys.exit(0)

vals = dict(cfg)

if mode_in == '2':
    for k, label in FIELDS:
        vals[k] = ask('  %s:' % label, vals.get(k, ''))
else:
    tty_print('请把控制台凭证区域整段复制粘贴到这里，最后按回车结束：')
    buf = []
    while True:
        line = tty.readline()
        if not line or line.strip() == '':
            break
        buf.append(line)
    text = ''.join(buf)
    # 兼容中英冒号/大小写/ShareThirdld(I/l 混写) 等控制台复制格式
    # 注意用 [ \t]* 而非 \s*：\s 会吞掉行尾换行，空值行会误捕下一行内容
    pat = re.compile(r'(?im)^[ \t]*(AppID|AppKey|SecretKey|SignKey|ShareSecret|ShareThird[Iil1]d)[ \t]*[:：][ \t]*(.*)$')
    # 控制台标签 → 配置键名（标签无下划线，配置键有下划线）
    label2key = {'appid': 'app_id', 'appkey': 'app_key', 'secretkey': 'secret_key',
                 'signkey': 'sign_key', 'sharesecret': 'share_secret'}
    got = {}
    for m in pat.finditer(text):
        key = m.group(1).lower()
        key = label2key.get(key, 'share_third_id')  # 剩余的只有 ShareThirdId 变体
        got[key] = m.group(2).strip().strip('"').strip("'")
    for k, label in FIELDS:
        if got.get(k):
            vals[k] = got[k]
    found = [label for k, label in FIELDS if got.get(k)]
    tty_print('  解析到: %s' % ('、'.join(found) if found else '（未识别到任何字段）'))

if not (vals.get('app_key') and vals.get('secret_key')):
    tty_print('⚠️  AppKey / SecretKey 缺失，本次不写入配置。可重新运行安装脚本再试。')
    sys.exit(0)

vals.setdefault('scope', 'basic,netdisk')
vals.setdefault('remote_root', '/apps')
vals.setdefault('share_third_id', '0')
with open(cfg_path, 'w', encoding='utf-8') as f:
    json.dump(vals, f, ensure_ascii=False, indent=2)

tty_print('✅ 配置已写入 %s' % cfg_path)
tty_print('   AppID=%s  AppKey=%s  SecretKey=%s  SignKey=%s' % (
    vals.get('app_id', '未填'), mask(vals.get('app_key')),
    mask(vals.get('secret_key')), mask(vals.get('sign_key'))))
PYEOF

# ---------------------------------------------------------------- 4. 冒烟测试
if python3 bpan.py --help >/dev/null 2>&1; then
  info "CLI 自检通过 ✓"
else
  die "bpan.py 自检失败，请把上面报错反馈到 https://github.com/toddpan/dsh-netdisk/issues"
fi

# ---------------------------------------------------------------- 5. 可选登录
do_login() {
  info "进入百度账号授权（浏览器打开链接 → 输入授权码，过期自动换新码，Ctrl+C 可退出）"
  python3 bpan.py login || warn "本次未完成授权，之后运行: cd $BPAN_INSTALL_DIR && python3 bpan.py login"
}

if [ "$NO_LOGIN" = "1" ]; then
  :
elif [ "$WANT_LOGIN" = "1" ]; then
  do_login
elif [ "$ASSUME_YES" != "1" ] && [ -t 0 ] && [ -e /dev/tty ]; then
  printf '%s' "${C_B}是否现在进行百度账号授权登录? [Y/n] ${C_0}"
  ans="n"
  if read -r ans </dev/tty 2>/dev/null; then :; fi
  case "$ans" in
    ""|y*|Y*) do_login ;;
    *)        info "跳过登录。之后运行: cd $BPAN_INSTALL_DIR && python3 bpan.py login" ;;
  esac
else
  info "跳过登录。之后运行: cd $BPAN_INSTALL_DIR && python3 bpan.py login"
fi

# ---------------------------------------------------------------- 6. 完成
printf '%s\n' ""
printf '%s\n' "${C_G}🎉 dsh-netdisk 安装完成！${C_0}"
printf '%s\n' "  目录:     $BPAN_INSTALL_DIR"
printf '%s\n' "  快速上手: cd $BPAN_INSTALL_DIR && python3 bpan.py whoami"
printf '%s\n' "  命令帮助: python3 bpan.py --help"
if [ -d "$HOME/.dsh/skills" ] && [[ "$BPAN_INSTALL_DIR" == "$HOME/.dsh/skills/"* ]]; then
  printf '%s\n' "  DSH 技能: 已就位，新会话中对 AI 说“把文件传到百度网盘”即可触发 baidu-netdisk 技能"
fi
printf '%s\n' "  ⚠️ 分享链接为百度付费能力，未开通时 share 会明确提示（详见 README）"
