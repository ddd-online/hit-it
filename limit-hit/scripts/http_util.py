#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""limit-hit 取数脚本共用的直连网络层。

约定：各脚本保留自己的 http_get（请求头、超时、解码方式各不相同），只共用本模块的
open_url 作为「谁去发这个请求」的唯一实现，并强制绕过系统代理。
"""

import json
import sys
import urllib.request

_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_JSON_EMITTED = {"value": False}


def mark_json_emitted():
    """脚本已经把 JSON 报告打到 stdout（随后仍要以非 0 退出码报错）时调用，避免再输出错误对象。"""
    _JSON_EMITTED["value"] = True


def fail(message):
    """失败统一出口：提示写 stderr，退出码 1；--json 时另把结构化错误打到 stdout（若还没打过 JSON）。"""
    text = str(message).rstrip("\n")
    sys.stderr.write(text + "\n")
    if "--json" in sys.argv and not _JSON_EMITTED["value"]:
        print(json.dumps({"error": text}, ensure_ascii=False))
    sys.exit(1)


def run(main_func):
    """脚本入口统一包装：中文报错、不抛 traceback，退出码语义一致。"""
    try:
        main_func()
    except KeyboardInterrupt:
        sys.exit("已中断。")
    except SystemExit as exc:
        if isinstance(exc.code, str):
            fail(exc.code)
        raise
    except Exception as exc:  # noqa: BLE001 统一报错，不静默
        fail("错误：{}".format(exc))


def open_url(req, timeout):
    """强制绕过系统代理，直连请求。"""
    return _DIRECT_OPENER.open(req, timeout=timeout)
