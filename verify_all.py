# -*- coding: utf-8 -*-
"""6 个服务的批量冒烟自测: 登录 -> 抓首页链接 -> 逐个 GET -> 断言状态码。

用法:
    G:/code/hermes_project/.venv/Scripts/python.exe verify_all.py
    ... verify_all.py 1 6        # 只测 1 和 6 号
输出: 每服务一行的通过/失败统计 + 失败明细, 退出码非 0 表示有失败。
"""
import http.cookiejar
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = 'http://127.0.0.1:%d'
PROJECTS = [
    (1, 8001, '图书借阅系统'),
    (2, 8002, '网上商城'),
    (3, 8003, '进销存 + 宅急送物流'),
    (4, 8004, '记账 + 数据看板'),
    (5, 8005, '多租户 SaaS 平台'),
    (6, 8006, 'AI 商品推荐助手'),
]
ACCOUNTS = [('admin', 'admin123'), ('ops', 'ops123'), ('demo', 'demo123')]
HREF_RE = re.compile(r'href="(/[^"#?]*)"')
SKIP = ('/logout', '/static/')


def make_opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def get(opener, url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': 'verify-all/1.0'})
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8', 'replace')
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, ''
    except Exception as exc:
        return 0, str(exc)


def post(opener, url, data, timeout=20):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        'User-Agent': 'verify-all/1.0',
        'Content-Type': 'application/x-www-form-urlencoded',
    })
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        return exc.code, ''
    except Exception as exc:
        return 0, str(exc)


def check_project(num, port, name):
    """返回 (passed, failed, details)。"""
    root = BASE % port
    opener = make_opener()
    st, body = get(opener, root + '/login')
    if st != 200:
        return 0, 1, ['GET /login -> %s' % (st or 'no-response')]
    st, body = post(opener, root + '/login', {'username': 'admin', 'password': 'admin123'})
    if st not in (200, 302):
        return 0, 1, ['POST /login -> %s (登录失败)' % st]

    pages, seen = [], set()
    for path in ['/'] + HREF_RE.findall(body):
        if path in seen or any(path.startswith(s) for s in SKIP):
            continue
        seen.add(path)
        pages.append(path)

    passed, failed, details = 0, 0, []
    for path in pages:
        st, _ = get(opener, root + path)
        if st in (200, 302):
            passed += 1
        else:
            failed += 1
            details.append('GET %s -> %s' % (path, st or 'no-response'))

    # 三个角色都能登录 (RBAC 入口不炸)
    for user, pwd in ACCOUNTS:
        op = make_opener()
        st, _ = post(op, root + '/login', {'username': user, 'password': pwd})
        if st in (200, 302):
            passed += 1
        else:
            failed += 1
            details.append('登录 %s -> %s' % (user, st))
    return passed, failed, details


def main():
    wanted = {int(a) for a in sys.argv[1:] if a.isdigit()}
    targets = [p for p in PROJECTS if not wanted or p[0] in wanted]
    total_fail = 0
    print('=' * 72)
    print('%-4s %-24s %-6s %-8s %s' % ('#', '项目', '端口', '结果', '通过/失败'))
    print('-' * 72)
    all_details = []
    for num, port, name in targets:
        passed, failed, details = check_project(num, port, name)
        total_fail += failed
        print('%-4d %-24s %-6d %-8s %d/%d' % (num, name, port,
                                              'PASS' if failed == 0 else 'FAIL',
                                              passed, passed + failed))
        for d in details:
            all_details.append('  [项目%d %s] %s' % (num, name, d))
    print('=' * 72)
    if all_details:
        print('失败明细:')
        for d in all_details:
            print(d)
    print('总结: %s' % ('全部通过' if total_fail == 0 else '%d 项失败' % total_fail))
    return 0 if total_fail == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
