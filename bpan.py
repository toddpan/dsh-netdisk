#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bpan.py — 百度网盘开放平台 CLI（基于官方 xpan Python SDK + 官方分享接口）

供 AI / 人在终端管理百度网盘：登录授权、列目录、上传、下载、搜索、
创建分享链接、文件增删改查。命令失败时以非零退出码 + stderr 说明退出。

用法示例：
  python3 bpan.py login                     # 设备码授权（浏览器打开链接，输入授权码）
  python3 bpan.py whoami                    # 当前用户与配额
  python3 bpan.py ls /apps/我的应用          # 列目录（含 fs_id）
  python3 bpan.py mkdir /apps/我的应用/备份   # 建目录（自动补齐父目录）
  python3 bpan.py upload ./a.zip /apps/我的应用/备份/a.zip
  python3 bpan.py download /apps/我的应用/a.zip ./out/
  python3 bpan.py share /apps/我的应用/a.zip --days 7     # 创建分享链接
  python3 bpan.py find 报告                  # 关键词搜索
  python3 bpan.py rm /apps/我的应用/a.zip    # 删除
  python3 bpan.py mv /a/1.zip /b/           # 移动；cp 复制；rename 改名
所有命令均支持 --json 输出原始 JSON。
"""
import sys
import os
import json
import time
import random
import hashlib
import argparse
import posixpath
import warnings
import webbrowser

warnings.filterwarnings('ignore', message='urllib3 v2 only supports OpenSSL')

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SKILL_DIR)

import requests  # noqa: E402
import urllib3  # noqa: E402
urllib3.disable_warnings()

import openapi_client  # noqa: E402
from openapi_client.api import (  # noqa: E402
    auth_api, fileinfo_api, fileupload_api,
    filemanager_api, multimediafile_api, userinfo_api,
)

CONFIG_PATH = os.path.join(SKILL_DIR, 'config.json')
TOKEN_PATH = os.path.join(SKILL_DIR, 'token.json')

PAN_HOST = 'https://pan.baidu.com'
PCS_HOST = 'https://d.pcs.baidu.com'
AUTH_HOST = 'https://openapi.baidu.com'
SHARE_SET_URL = 'https://pan.baidu.com/apaas/1.0/share/set'
CHUNK_SIZE = 4 * 1024 * 1024  # xpan 分片固定 4MB
DOWNLOAD_UA = 'pan.baidu.com'  # 官方要求的下载 UA

ERRNO_HINT = {
    -6: '身份验证失败（access_token 无效或过期）',
    -7: '文件或目录不存在',
    -8: '文件或目录已存在',
    -9: '无权访问该文件或目录（第三方应用只能访问 /apps/<应用名>/ 下的文件）',
    -10: '网盘接口请求次数超限',
    -70: '文件名含有敏感词，无法分享',
    2: '参数错误',
    12: '网盘容量不足',
    111: 'access_token 已过期，请刷新或重新登录',
    31062: '文件名非法',
    31064: '文件已存在',
    31066: '文件或目录不存在',
    31079: '未找到文件（查 path 是否存在于网盘）',
    13998: '应用未开通分享服务（文件分享服务为企业付费能力，需购买开通）',
}


class BpanError(Exception):
    """业务失败，message 直接面向使用者。"""


# ---------------------------------------------------------------- 基础设施

def load_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (IOError, ValueError):
        return default


def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config():
    cfg = load_json(CONFIG_PATH)
    if not cfg or not cfg.get('app_key'):
        raise BpanError('config.json 缺失或没有 app_key：%s' % CONFIG_PATH)
    return cfg


def load_token():
    return load_json(TOKEN_PATH)


def require_token():
    tok = load_token()
    if not tok or not tok.get('access_token'):
        raise BpanError('尚未登录百度账号：请先运行 `python3 %s login` 完成设备码授权'
                        % os.path.basename(__file__))
    # 距过期不足 1 天时自动刷新（access_token 有效期一般 30 天）
    if tok.get('expires_at', 0) - time.time() < 86400 and tok.get('refresh_token'):
        try:
            do_refresh(tok)
        except Exception:
            pass  # 刷新失败不阻塞，等 API 报 -6 再处理
    return tok['access_token']


def refresh_access_token():
    """强制刷新并保存；失败抛 BpanError。"""
    tok = load_token()
    if not tok or not tok.get('refresh_token'):
        raise BpanError('没有 refresh_token，请重新运行 login')
    do_refresh(tok)
    return tok['access_token']


def do_refresh(tok):
    cfg = load_config()
    conf = openapi_client.Configuration(host=AUTH_HOST)
    with openapi_client.ApiClient(conf) as client:
        resp = auth_api.AuthApi(client).oauth_token_refresh_token(
            tok['refresh_token'], cfg['app_key'], cfg['secret_key'])
    data = model_to_dict(resp)
    if not data.get('access_token'):
        raise BpanError('刷新 token 失败：%s' % json.dumps(data, ensure_ascii=False))
    tok.update({
        'access_token': data['access_token'],
        'expires_at': time.time() + int(data.get('expires_in', 2592000)),
        'scope': data.get('scope', tok.get('scope', '')),
        'updated_at': time.time(),
    })
    if data.get('refresh_token'):
        tok['refresh_token'] = data['refresh_token']
    save_json(TOKEN_PATH, tok)


def model_to_dict(resp):
    if resp is None:
        return {}
    if isinstance(resp, dict):
        return resp
    if hasattr(resp, 'to_dict'):
        return resp.to_dict()
    return dict(resp)


def api_client():
    return openapi_client.ApiClient(openapi_client.Configuration(host=PAN_HOST))


def call_sdk(func, *args, **kwargs):
    """调用 SDK 方法；遇到 -6/token 失效自动刷新后重试一次。"""
    try:
        return func(*args, **kwargs)
    except openapi_client.ApiException as e:
        if is_token_error(e):
            refresh_access_token()
            kwargs['access_token'] = load_token()['access_token']
            return func(*args, **kwargs)
        raise to_bpan_error(e)


def is_token_error(e):
    if getattr(e, 'status', None) in (401, 403):
        return True
    try:
        body = json.loads(e.body)
        return int(body.get('errno', 1)) in (-6, 111)
    except Exception:
        return False


def to_bpan_error(e):
    hint = ''
    try:
        body = json.loads(e.body)
        errno = body.get('errno')
        hint = 'errno=%s（%s）' % (errno, ERRNO_HINT.get(errno, '未知错误'))
    except Exception:
        hint = 'HTTP %s' % getattr(e, 'status', '?')
    return BpanError('百度网盘接口调用失败：%s' % hint)


def check_errno(data, action=''):
    if isinstance(data, dict) and int(data.get('errno', 0)) != 0:
        errno = int(data['errno'])
        raise BpanError('%s失败 errno=%s（%s）%s'
                        % (action, errno, ERRNO_HINT.get(errno, '未知错误'),
                           data.get('show_msg') or ''))
    return data


# ---------------------------------------------------------------- 认证

def cmd_login(args):
    cfg = load_config()
    # 设备码有效期仅 5 分钟，过期自动换新码继续等待（最多换 30 次 ≈ 2.5 小时）
    for attempt in range(30):
        r = requests.get(AUTH_HOST + '/oauth/2.0/device/code', params={
            'response_type': 'device_code',
            'client_id': cfg['app_key'],
            'scope': cfg.get('scope', 'basic,netdisk'),
        }, timeout=30)
        data = r.json()
        if r.status_code != 200 or 'device_code' not in data:
            raise BpanError('获取设备码失败：%s' % json.dumps(data, ensure_ascii=False))

        verify_url = data.get('verification_url', 'https://openapi.baidu.com/device')
        print('=' * 60)
        print('百度网盘账号授权（设备码模式）第 %d 个授权码 %s'
              % (attempt + 1, time.strftime('%H:%M:%S')))
        print('-' * 60)
        print('1. 用浏览器打开：%s' % verify_url)
        print('2. 登录百度账号，输入授权码：%s' % data['user_code'])
        print('3. 点击授权后，本命令会自动完成登录')
        if data.get('qrcode_url'):
            print('   （也可扫码：%s）' % data['qrcode_url'])
        print('   授权码 5 分钟内有效，过期会自动换新码')
        print('=' * 60)
        if attempt == 0:
            try:
                webbrowser.open(verify_url)
            except Exception:
                pass

        interval = int(data.get('interval', 5))
        deadline = time.time() + int(data.get('expires_in', 300))
        result = None
        while time.time() < deadline:
            time.sleep(interval)
            pr = requests.get(AUTH_HOST + '/oauth/2.0/token', params={
                'grant_type': 'device_token',
                'code': data['device_code'],
                'client_id': cfg['app_key'],
                'client_secret': cfg['secret_key'],
            }, timeout=30)
            body = pr.json()
            if pr.status_code == 200 and body.get('access_token'):
                save_json(TOKEN_PATH, {
                    'access_token': body['access_token'],
                    'refresh_token': body.get('refresh_token', ''),
                    'expires_at': time.time() + int(body.get('expires_in', 2592000)),
                    'scope': body.get('scope', ''),
                    'updated_at': time.time(),
                })
                print('✅ 授权成功，令牌已保存到 %s' % TOKEN_PATH)
                cmd_whoami(args)
                return
            err = body.get('error', '')
            if err == 'authorization_pending':
                continue  # 等待用户授权
            if err == 'slow_down':
                interval += 5
                continue
            result = json.dumps(body, ensure_ascii=False)
            break  # 过期/被拒：尝试换新码
        if result:
            print('⏳ 本授权码不可用（%s），自动换新码…' % result)
    raise BpanError('设备码多次过期仍未完成授权，请确认浏览器操作后重新运行 login')


def cmd_logout(_args):
    if os.path.exists(TOKEN_PATH):
        os.remove(TOKEN_PATH)
    print('✅ 已清除本地令牌')


# ---------------------------------------------------------------- 用户/配额

def cmd_whoami(args):
    token = require_token()
    with api_client() as client:
        uinfo = check_errno(model_to_dict(call_sdk(
            userinfo_api.UserinfoApi(client).xpannasuinfo, token)), '获取用户信息')
        quota = check_errno(model_to_dict(call_sdk(
            userinfo_api.UserinfoApi(client).apiquota, token)), '获取配额')
    out = {
        'baidu_name': uinfo.get('baidu_name'),
        'netdisk_name': uinfo.get('netdisk_name'),
        'vip_type': uinfo.get('vip_type'),
        'quota_total_gb': round(int(quota.get('total', 0)) / 1024 ** 3, 2),
        'quota_used_gb': round(int(quota.get('used', 0)) / 1024 ** 3, 2),
        'quota_free_gb': round((int(quota.get('total', 0)) - int(quota.get('used', 0)))
                               / 1024 ** 3, 2),
    }
    emit(args, out,
         '👤 {baidu_name}（网盘名 {netdisk_name}，vip_type={vip_type}）'.format(**out))
    print('   容量：已用 %s GB / 共 %s GB（剩余 %s GB）'
          % (out['quota_used_gb'], out['quota_total_gb'], out['quota_free_gb']))


def cmd_quota(args):
    cmd_whoami(args)


# ---------------------------------------------------------------- 列表/查找

def human_size(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return ('%.1f %s' % (n, unit)) if unit != 'B' else ('%d B' % n)
        n /= 1024.0


def fmt_entry(e):
    isdir = int(e.get('isdir', 0))
    mtime = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(e.get('server_mtime', 0))))
    return {'fs_id': str(e.get('fs_id', '')), 'path': e.get('path', ''),
            'name': e.get('server_filename') or posixpath.basename(e.get('path', '')),
            'isdir': isdir, 'size': int(e.get('size', 0)),
            'size_human': '目录' if isdir else human_size(e.get('size')),
            'md5': e.get('md5', ''), 'mtime': mtime,
            'category': e.get('category', 0)}


def list_dir(path, args=None):
    """method=list 分页列取 path 的直接子项。"""
    token = require_token()
    with api_client() as client:
        api = fileinfo_api.FileinfoApi(client)
        items, start = [], 0
        while True:
            data = check_errno(model_to_dict(call_sdk(
                api.xpanfilelist, token, dir=path, start=str(start),
                limit=1000, order='name', desc=0, web='1')), '列目录 %s' % path)
            batch = data.get('list') or []
            items.extend(batch)
            if len(batch) < 1000:
                break
            start += len(batch)
    return [fmt_entry(e) for e in items]


def list_all(path):
    """listall 拉取（仅网盘根路径可用；/apps 下会报 20020，勿用于应用目录）。"""
    token = require_token()
    with api_client() as client:
        api = multimediafile_api.MultimediafileApi(client)
        items, start = [], 0
        while True:
            data = check_errno(model_to_dict(call_sdk(
                api.xpanfilelistall, token, path=path, recursion=0,
                web='1', start=str(start), limit=1000)), '递归列表 %s' % path)
            batch = data.get('list') or []
            items.extend(batch)
            if len(batch) < 1000:
                break
            start += len(batch)
    return [fmt_entry(e) for e in items]


def cmd_ls(args):
    path = normalize_remote(args.path)
    items = list_dir(path)
    for it in items:
        it['size_human'] = '目录' if it['isdir'] else it['size_human']
    emit(args, items, None)
    if not args.json:
        if not items:
            print('（空目录）%s' % path)
            return
        print('%-4s %-10s %-17s %-20s %s' % ('类型', '大小', '修改时间', 'fs_id', '名称'))
        for it in items:
            print('%-4s %-10s %-17s %-20s %s'
                  % ('目录' if it['isdir'] else '文件', it['size_human'],
                     it['mtime'], it['fs_id'], it['name']))


def cmd_tree(args):
    root = normalize_remote(args.path)
    rows = list_recursive(root)
    emit(args, rows, None)
    if not args.json:
        if not rows:
            print('（空目录）%s' % root)
            return
        for it in rows:
            depth = it['path'].rstrip('/').count('/') - root.rstrip('/').count('/')
            label = it['name'] + ('/' if it['isdir'] else '')
            print('%s%s  (%s)' % ('  ' * max(depth, 0), label,
                                  '目录' if it['isdir'] else it['size_human']))


def list_recursive(root):
    """逐层 method=list 递归（官方 listall 接口不允许 /apps 下的路径）。"""
    rows = []
    stack = [root]
    while stack:
        cur = stack.pop()
        for it in list_dir(cur):
            rows.append(it)
            if it['isdir']:
                stack.append(it['path'])
    return rows


def cmd_find(args):
    token = require_token()
    # SDK 封装的 search 会报 errno=2，改用与官方文档一致的原生 GET
    r = requests.get(PAN_HOST + '/rest/2.0/xpan/file', params={
        'method': 'search', 'access_token': token, 'key': args.keyword,
        'dir': normalize_remote(args.dir), 'recursion': '1',
        'num': '100', 'web': '1',  # 注意：此接口传 page=0 会报 errno=2
    }, timeout=60)
    try:
        data = r.json()
    except ValueError:
        raise BpanError('搜索失败 HTTP %s：%s' % (r.status_code, r.text[:200]))
    check_errno(data, '搜索')
    items = [fmt_entry(e) for e in (data.get('list') or [])]
    emit(args, items, None)
    if not args.json:
        if not items:
            print('（未找到匹配 “%s” 的文件）' % args.keyword)
            return
        for it in items:
            print('%-10s %-17s %s' % (it['size_human'] if not it['isdir'] else '目录',
                                      it['mtime'], it['path']))


def cmd_stat(args):
    token = require_token()
    entry = resolve_path(normalize_remote(args.path))
    with api_client() as client:
        data = check_errno(model_to_dict(call_sdk(
            multimediafile_api.MultimediafileApi(client).xpanmultimediafilemetas,
            token, json.dumps([int(entry['fs_id'])]),
            dlink='1' if args.dlink else '0',
            thumb='0', extra='0')), '查询文件信息')
    out = dict(entry)
    metas = (data.get('list') or [{}])[0]
    if metas:
        out['dlink'] = metas.get('dlink')
        out['filename'] = metas.get('filename')
    emit(args, out, None)
    if not args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))


def resolve_path(path):
    """把完整路径解析成含 fs_id 的条目（ls 父目录匹配）。"""
    if path in ('/', ''):
        raise BpanError('根目录不支持该操作，请使用 /apps/… 下的具体文件或目录')
    parent = posixpath.dirname(path.rstrip('/')) or '/'
    name = posixpath.basename(path.rstrip('/'))
    for it in list_dir(parent):
        if it['name'] == name or it['path'].rstrip('/') == path.rstrip('/'):
            return it
    raise BpanError('网盘中不存在：%s（请先用 ls 确认路径）' % path)


# ---------------------------------------------------------------- 目录创建

def raw_create_dir(path):
    """创建单个目录（SDK 未单独提供，走官方 method=create&isdir=1）。"""
    token = require_token()
    r = requests.post(PAN_HOST + '/rest/2.0/xpan/file',
                      params={'method': 'create'},
                      data={'access_token': token, 'path': path,
                            'isdir': 1, 'rtype': 1},
                      timeout=60)
    try:
        data = r.json()
    except ValueError:
        raise BpanError('创建目录失败：HTTP %s %s' % (r.status_code, r.text[:200]))
    errno = int(data.get('errno', 0))
    if errno not in (0, -8):  # -8 = 已存在，视为成功
        raise BpanError('创建目录 %s 失败 errno=%s（%s）'
                        % (path, errno, ERRNO_HINT.get(errno, '未知错误')))
    return data.get('fs_id')


def cmd_mkdir(args):
    path = normalize_remote(args.path)
    made = []
    cur = ''
    for part in [p for p in path.split('/') if p]:
        cur += '/' + part
        if cur == '/apps':
            continue  # 一级目录 /apps 无需也无法创建
        try:
            raw_create_dir(cur)
            made.append(cur)
        except BpanError as e:
            if 'errno=-8' not in str(e):
                raise
    emit(args, {'path': path, 'created': made}, '✅ 目录就绪：%s%s'
         % (path, ('（新建了 %s）' % ', '.join(made)) if made else '（已存在）'))


# ---------------------------------------------------------------- 上传

def cmd_upload(args):
    local = os.path.abspath(args.local)
    if not os.path.isfile(local):
        raise BpanError('本地文件不存在：%s' % local)
    remote = args.remote
    if remote:
        remote = normalize_remote(remote)
        # 若 remote 以 / 结尾或 remote 是已存在目录，则拼上本地文件名
        if remote.endswith('/') or _is_dir(remote):
            remote = remote.rstrip('/') + '/' + os.path.basename(local)
    else:
        remote = normalize_remote(load_config().get('remote_root', '/apps')
                                  + '/' + os.path.basename(local))
    parent = posixpath.dirname(remote) or '/'
    if parent != '/':
        raw_mkdir_p(parent)
    if parent == '/apps' or not parent.startswith('/apps'):
        print('⚠️  提示：第三方应用通常只能写入 /apps/<应用名>/ 目录，若报 -9/-8 权限错误请调整目标路径',
              file=sys.stderr)

    size = os.path.getsize(local)
    chunks, block_list = [], []
    with open(local, 'rb') as f:
        while True:
            buf = f.read(CHUNK_SIZE)
            if not buf:
                break
            chunks.append(buf)
            block_list.append(hashlib.md5(buf).hexdigest())

    token = require_token()
    with api_client() as client:
        up = fileupload_api.FileuploadApi(client)
        pre = check_errno(model_to_dict(call_sdk(
            up.xpanfileprecreate, token, remote, 0, size, 1,
            json.dumps(block_list), rtype=args.rtype)), '预上传 %s' % remote)
    uploadid = pre.get('uploadid')
    # precreate 响应的 block_list 是「需要上传的分片下标」数组（如 [0,2]）
    need_idx = set()
    for x in (pre.get('block_list') or []):
        if isinstance(x, int):
            need_idx.add(x)
        else:
            try:
                need_idx.add(int(x))
            except (TypeError, ValueError):
                need_idx = set(range(len(chunks)))  # 非 int 视为全部上传
                break
    if int(pre.get('return_type', 1)) == 2:
        print('⚡ 秒传：文件已存在于网盘，无需传输')
    else:
        # 分片上传必须走 d.pcs.baidu.com 域名（pan.baidu.com 会 403，
        # 且响应头误标 gzip，需 Accept-Encoding: identity 规避解码崩溃）
        uploaded = 0
        for i, buf in enumerate(chunks):
            if need_idx and i not in need_idx:
                uploaded += len(buf)
                continue  # 网盘已有该分片（秒传部分命中），跳过
            with requests.post(
                    PCS_HOST + '/rest/2.0/pcs/superfile2',
                    params={'method': 'upload', 'access_token': token,
                            'type': 'tmpfile', 'path': remote,
                            'uploadid': uploadid, 'partseq': str(i)},
                    files={'file': (os.path.basename(remote), buf,
                                    'application/octet-stream')},
                    headers={'Accept-Encoding': 'identity'},
                    timeout=300) as resp:
                if resp.status_code != 200:
                    raise BpanError('上传分片 %s 失败 HTTP %s：%s'
                                    % (i, resp.status_code, resp.text[:200]))
                body = resp.json()
                if int(body.get('errno', 0)) != 0:
                    raise BpanError('上传分片 %s 失败 errno=%s'
                                    % (i, body.get('errno')))
            uploaded += len(buf)
            if not args.json:
                pct = min(100, uploaded * 100 // max(size, 1))
                sys.stdout.write('\r   上传中 %d%%（%s / %s）'
                                 % (pct, human_size(uploaded), human_size(size)))
                sys.stdout.flush()
        if not args.json:
            print()
    with api_client() as client:
        created = check_errno(model_to_dict(call_sdk(
            fileupload_api.FileuploadApi(client).xpanfilecreate, token, remote,
            0, size, uploadid, json.dumps(block_list), rtype=args.rtype)),
            '创建文件 %s' % remote)

    out = {'local': local, 'remote': remote, 'size': size,
           'size_human': human_size(size), 'fs_id': str(created.get('fs_id', ''))}
    emit(args, out, '✅ 上传完成：%s → %s（%s）'
         % (local, remote, out['size_human']))


def _is_dir(path):
    try:
        return bool(resolve_path(path)['isdir'])
    except BpanError:
        return False


def raw_mkdir_p(path):
    cur = ''
    for part in [p for p in path.split('/') if p]:
        cur += '/' + part
        if cur == '/apps':
            continue  # 一级目录 /apps 无需也无法创建
        try:
            raw_create_dir(cur)
        except BpanError as e:
            if 'errno=-8' not in str(e):
                raise


# ---------------------------------------------------------------- 下载

def cmd_download(args):
    token = require_token()
    remote = normalize_remote(args.remote)
    entry = resolve_path(remote)
    if entry['isdir']:
        raise BpanError('%s 是目录，请逐个下载其中文件（先 ls）' % remote)

    with api_client() as client:
        data = check_errno(model_to_dict(call_sdk(
            multimediafile_api.MultimediafileApi(client).xpanmultimediafilemetas,
            token, json.dumps([int(entry['fs_id'])]), dlink='1',
            thumb='0', extra='0')), '获取下载链接')
    dlink = ((data.get('list') or [{}])[0]).get('dlink')
    if not dlink:
        raise BpanError('未获取到 dlink（可能无下载权限）')

    if os.path.isdir(args.local) or args.local.endswith('/'):
        target = os.path.join(args.local or '.', entry['name'])
    else:
        target = os.path.abspath(args.local) if args.local else os.path.join(os.getcwd(), entry['name'])
    os.makedirs(os.path.dirname(target) or '.', exist_ok=True)

    with requests.get(dlink, params={'access_token': token},
                      headers={'User-Agent': DOWNLOAD_UA},
                      stream=True, allow_redirects=True, timeout=300) as r:
        if r.status_code != 200:
            raise BpanError('下载失败 HTTP %s：%s' % (r.status_code, r.text[:200]))
        done = 0
        with open(target, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                f.write(chunk)
                done += len(chunk)
                if not args.json and entry['size']:
                    sys.stdout.write('\r   下载中 %d%%（%s / %s）'
                                     % (min(100, done * 100 // entry['size']),
                                        human_size(done), human_size(entry['size'])))
                    sys.stdout.flush()
    if not args.json:
        print()
    out = {'remote': remote, 'local': target, 'size': entry['size'],
           'size_human': human_size(entry['size'])}
    emit(args, out, '✅ 下载完成：%s → %s（%s）'
         % (remote, target, out['size_human']))


# ---------------------------------------------------------------- 分享

def cmd_share(args):
    token = require_token()
    cfg = load_config()
    if not args.paths:
        raise BpanError('请指定要分享的网盘路径')
    fsids, names = [], []
    for p in args.paths:
        entry = resolve_path(normalize_remote(p))
        fsids.append(str(entry['fs_id']))
        names.append(entry['name'])

    pwd = args.pwd or ''.join(random.choice('abcdefghjkmnpqrstuvwxyz23456789')
                              for _ in range(4))
    form = {
        'fsid_list': (None, json.dumps(fsids)),  # 官方要求：字符串数组 JSON
        'period': (None, str(args.days)),
        'pwd': (None, pwd),
    }
    if args.remark:
        form['remark'] = (None, args.remark)

    r = requests.post(SHARE_SET_URL, params={
        'product': 'netdisk',
        'appid': cfg.get('app_id', ''),
        'access_token': token,
    }, files=form, timeout=60)
    try:
        data = r.json()
    except ValueError:
        raise BpanError('分享接口返回异常 HTTP %s：%s' % (r.status_code, r.text[:300]))
    try:
        check_errno(data, '创建分享')
    except BpanError as e:
        if '13998' in str(e) or 'invalid app' in str(e):
            raise BpanError(
                '该应用（appid=%s）未开通官方「文件分享服务（新）」。'
                '这是百度开放平台的企业开发者付费能力：需完成企业实名认证并购买服务后'
                '才能通过 API 创建分享链接（咨询 ext_mars-union@baidu.com，1-3 个工作日回复；'
                '旧版分享接口已下线）。替代方案：用 download 命令把文件下载到本地后直接发给用户。'
                % cfg.get('app_id', ''))
        raise

    d = data.get('data') or {}
    out = {
        'link': d.get('link') or d.get('short_url'),
        'short_url': d.get('short_url'),
        'pwd': d.get('pwd', pwd),
        'period_days': d.get('period', args.days),
        'share_id': str(d.get('share_id', '')),
        'files': names,
    }
    emit(args, out, None)
    if not args.json:
        print('✅ 分享创建成功')
        for n in names:
            print('   文件：%s' % n)
        print('🔗 链接：%s' % out['link'])
        print('🔑 提取码：%s' % out['pwd'])
        print('⏳ 有效期：%s 天' % out['period_days'])
        print('（请把上面 链接 + 提取码 一起发给用户）')


# ---------------------------------------------------------------- 文件管理

def _filemanager(args, opera, filelist, action, ondup='fail'):
    token = require_token()
    with api_client() as client:
        api = filemanager_api.FilemanagerApi(client)
        method = {'delete': api.filemanagerdelete, 'move': api.filemanagermove,
                  'copy': api.filemanagercopy, 'rename': api.filemanagerrename}[opera]
        data = check_errno(model_to_dict(call_sdk(
            method, token, 0, json.dumps(filelist), ondup=ondup)), action)
    info = data.get('info') or []
    for it in info:
        it.pop('from_fs_id', None)
    return info


def cmd_rm(args):
    items = [_norm_for_manager(p) for p in args.paths]
    info = _filemanager(args, 'delete', [{'path': p} for p in items], '删除')
    emit(args, {'deleted': items, 'info': info}, '✅ 已删除 %d 项：\n   %s'
         % (len(items), '\n   '.join(items)))


def cmd_mv(args):
    src = _norm_for_manager(args.src)
    if _is_dir(normalize_remote(args.dst)):
        dst_dir, newname = normalize_remote(args.dst), posixpath.basename(src)
    else:
        dst_dir = posixpath.dirname(normalize_remote(args.dst))
        newname = posixpath.basename(normalize_remote(args.dst))
    info = _filemanager(args, 'move',
                        [{'path': src, 'dest': dst_dir, 'newname': newname}],
                        '移动', ondup=args.ondup)
    final = dst_dir.rstrip('/') + '/' + newname
    emit(args, {'src': src, 'dest': final, 'info': info}, '✅ 已移动：%s → %s' % (src, final))


def cmd_cp(args):
    src, dst = _norm_for_manager(args.src), args.dst
    if _is_dir(normalize_remote(dst)):
        dst_dir, newname = normalize_remote(dst), posixpath.basename(src)
    else:
        dst_dir = posixpath.dirname(normalize_remote(dst))
        newname = posixpath.basename(normalize_remote(dst))
    info = _filemanager(args, 'copy',
                        [{'path': src, 'dest': dst_dir, 'newname': newname}],
                        '复制', ondup=args.ondup)
    final = dst_dir.rstrip('/') + '/' + newname
    emit(args, {'src': src, 'dest': final, 'info': info}, '✅ 已复制：%s → %s' % (src, final))


def cmd_rename(args):
    path, newname = _norm_for_manager(args.path), args.newname
    info = _filemanager(args, 'rename', [{'path': path, 'newname': newname}], '重命名')
    final = posixpath.dirname(path) + '/' + newname
    emit(args, {'old': path, 'new': final, 'info': info}, '✅ 已重命名：%s → %s' % (path, final))


def _norm_for_manager(p):
    return normalize_remote(p).rstrip('/') or '/'


# ---------------------------------------------------------------- 工具

def normalize_remote(path):
    """统一为以 / 开头的绝对路径；支持以 ~/ 或相对远端根目录书写的路径。"""
    path = (path or '').strip()
    if not path:
        return '/'
    if path.startswith('~'):
        path = load_config().get('remote_root', '/apps') + path[1:]
    if not path.startswith('/'):
        path = load_config().get('remote_root', '/apps').rstrip('/') + '/' + path
    norm = posixpath.normpath(path)
    return norm if norm != '/' else '/'


def emit(args, data, text):
    """--json 输出 JSON；否则输出可读文本（text 为 None 时不额外打印）。"""
    if getattr(args, 'json', False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif text:
        print(text)


def main():
    parser = argparse.ArgumentParser(prog='bpan', description='百度网盘开放平台 CLI')
    parser.add_argument('--json', action='store_true', help='以 JSON 输出结果')
    sub = parser.add_subparsers(dest='cmd', required=True)

    sub.add_parser('login', help='设备码授权登录').set_defaults(func=cmd_login)
    sub.add_parser('logout', help='清除本地令牌').set_defaults(func=cmd_logout)
    p = sub.add_parser('whoami', help='当前用户与网盘配额')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_whoami)
    p = sub.add_parser('quota', help='同 whoami')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_quota)

    p = sub.add_parser('ls', help='列出目录（含 fs_id）')
    p.add_argument('path', nargs='?', default='~/')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser('tree', help='递归展示目录树')
    p.add_argument('path', nargs='?', default='~/')
    p.add_argument('--recursion', action='store_true', help='兼容参数（默认已递归）')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser('find', help='关键词搜索文件')
    p.add_argument('keyword')
    p.add_argument('--dir', default='~/')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_find)

    p = sub.add_parser('stat', help='查看文件/目录详情（含 fs_id，可带 dlink）')
    p.add_argument('path')
    p.add_argument('--dlink', action='store_true', help='同时返回下载直链')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_stat)

    p = sub.add_parser('mkdir', help='创建目录（自动补齐父目录）')
    p.add_argument('path')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_mkdir)

    p = sub.add_parser('upload', help='上传本地文件（分片 + 秒传）')
    p.add_argument('local', help='本地文件路径')
    p.add_argument('remote', nargs='?', help='远端路径或目录（缺省放到 /apps 根）')
    p.add_argument('--rtype', type=int, default=2, choices=[0, 1, 2, 3],
                   help='同名策略：0 改名 1 报错 2 覆盖(默认) 3 已存在即成功')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser('download', help='下载网盘文件到本地')
    p.add_argument('remote', help='网盘文件路径')
    p.add_argument('local', nargs='?', default='.', help='本地目录或文件路径')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_download)

    p = sub.add_parser('share', help='创建分享链接（官方分享服务）')
    p.add_argument('paths', nargs='+', help='要分享的网盘路径（可多个）')
    p.add_argument('--days', type=int, default=7, help='有效期（天，默认 7）')
    p.add_argument('--pwd', help='4 位提取码（数字+小写字母，缺省随机）')
    p.add_argument('--remark', help='分享备注')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_share)

    p = sub.add_parser('rm', help='删除文件/目录')
    p.add_argument('paths', nargs='+')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser('mv', help='移动文件/目录')
    p.add_argument('src')
    p.add_argument('dst', help='目标目录或目标完整路径')
    p.add_argument('--ondup', default='fail', choices=['fail', 'overwrite', 'newcopy'])
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_mv)

    p = sub.add_parser('cp', help='复制文件/目录')
    p.add_argument('src')
    p.add_argument('dst', help='目标目录或目标完整路径')
    p.add_argument('--ondup', default='fail', choices=['fail', 'overwrite', 'newcopy'])
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_cp)

    p = sub.add_parser('rename', help='重命名')
    p.add_argument('path')
    p.add_argument('newname')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_rename)

    args = parser.parse_args()
    try:
        args.func(args)
    except BpanError as e:
        print('❌ %s' % e, file=sys.stderr)
        sys.exit(1)
    except openapi_client.ApiException as e:
        print('❌ %s' % to_bpan_error(e), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('\n（已中断）', file=sys.stderr)
        sys.exit(130)


if __name__ == '__main__':
    main()
