# -*- coding: utf-8 -*-
"""issue #770 守卫：存量嵌套根重复的**只读检测**（`ref action=check` 的 nested_duplicate）。

## 病象
#754 的入口守卫只拦**新增**嵌套根；守卫上线前 `doc_*` / `code_*` 已双写的节点仍在库里
（报告者实测 93 份 ×3 遍、2971 冗余切块 / 8 MB），检索照样返回重复内容。

## 本守卫断言面
- D1 父根索引成功（正常路径）；
- D2 同一物理文件再从**子根**索引会被 #754 守卫拒 ⇒ 用 `allow_nested=true` 才能造出存量双写
     （这条同时证明：nested_duplicate 检测的是**守卫之前/被放行**留下的数据）；
- D3 `ref action=check` 回执带 `nested_duplicate`：count=1、files=1，且
     entry 的 root=内侧根、outer_root=外侧根、realpath=同一物理文件、nodes 指向内侧那套节点；
- D4 反例：两个**互不包含**的根各自索引各自的文件 ⇒ 不计入（不误伤多源根合法多域 #352-E）；
- D5 只读性：检测前后节点数与 `_refindex.json` 字节数都不变（不删节点、不写水位）。

运行：python -X utf8 -m md_cg.test_issue770_nested_duplicate_detect
"""
import json
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
_TMP = tempfile.mkdtemp(prefix="iss770_")
os.environ.update({
    "MDCG_INGEST_ROOT": _TMP,
    "MDCG_SUSTAIN": "0",
    "MDCG_SLEEP": "0",
})

from md_cg import mcp_server                       # noqa: E402
from md_cg.mdcos import MdCGOS                     # noqa: E402

FAILS = []


def ok(cond, name, detail=""):
    if cond:
        print("  PASS " + name)
    else:
        print("  FAIL " + name + "  " + str(detail)[:300])
        FAILS.append(name)


def _lib(tag):
    return MdCGOS(os.path.join(_TMP, "brain_" + tag), autoflush=1)


def _call(cg, **a):
    r = mcp_server.call_tool(cg, "cg", a)
    if isinstance(r, dict) and "content" in r:
        return json.loads(r["content"][0]["text"])
    return r


def _nodes(cg):
    return len((cg.index.get("nodes") or {}))


def _ledger_bytes(cg):
    fp = os.path.join(cg.root, "_refindex.json")
    if not os.path.exists(fp):
        return -1
    return os.path.getsize(fp)


def _write(fp, text):
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def main():
    cg = _lib("nested")
    outer = os.path.join(_TMP, "domain")
    inner = os.path.join(outer, "sub")

    # 同一物理文件：outer/sub/a.md —— 父根看是 sub/a.md，子根看是 a.md
    _write(os.path.join(inner, "a.md"), "# 甲\n\n内容一。\n\n## 小节\n\n内容二。\n")

    # D1 父根索引
    r1 = _call(cg, op="index_doc", path=outer)
    ok(r1.get("indexed", 0) >= 1, "D1 父根索引成功", r1)

    # D2 子根默认被 #754 拦；allow_nested 才放行（造存量双写）
    r2 = _call(cg, op="index_doc", path=inner)
    ok(r2.get("ok") is False or r2.get("error") == "nested_root",
       "D2 子根默认被 #754 守卫拒", r2)
    r3 = _call(cg, op="index_doc", path=inner, allow_nested=True)
    ok(r3.get("indexed", 0) >= 1, "D2b allow_nested=true 放行（造出存量双写）", r3)

    n_before, led_before = _nodes(cg), _ledger_bytes(cg)

    # D3 检测读数
    chk = _call(cg, op="ref", action="check")
    nd = chk.get("nested_duplicate") or {}
    ok(nd.get("count") == 1, "D3a nested_duplicate.count == 1", nd)
    ok(nd.get("files") == 1, "D3b nested_duplicate.files == 1", nd)
    ent = (nd.get("entries") or [{}])[0]
    ok(os.path.normcase(os.path.abspath(ent.get("root") or "")) ==
       os.path.normcase(os.path.abspath(inner)), "D3c entry.root == 内侧根", ent)
    ok(os.path.normcase(os.path.abspath(ent.get("outer_root") or "")) ==
       os.path.normcase(os.path.abspath(outer)), "D3d entry.outer_root == 外侧根", ent)
    ok(os.path.normcase(ent.get("realpath") or "") ==
       os.path.normcase(os.path.realpath(os.path.join(inner, "a.md"))),
       "D3e entry.realpath == 同一物理文件", ent)
    ok(isinstance(ent.get("nodes"), list) and len(ent["nodes"]) == 1,
       "D3f entry.nodes 指向内侧那套节点（1 个）", ent)

    # D4 反例：互不包含的两个根，各自索引各自的文件 ⇒ 不计入
    a_root = os.path.join(_TMP, "domainA")
    b_root = os.path.join(_TMP, "domainB")
    _write(os.path.join(a_root, "x.md"), "# A\n\n甲域内容。\n")
    _write(os.path.join(b_root, "x.md"), "# B\n\n乙域内容。\n")
    _call(cg, op="index_doc", path=a_root)
    _call(cg, op="index_doc", path=b_root)
    n_before, led_before = _nodes(cg), _ledger_bytes(cg)
    chk2 = _call(cg, op="ref", action="check")
    nd2 = chk2.get("nested_duplicate") or {}
    ok(nd2.get("count") == 1,
       "D4 互不包含的多源根不误判（count 仍为 1）", nd2)
    roots_hit = {os.path.normcase(os.path.abspath(e.get("root") or ""))
                 for e in (nd2.get("entries") or [])}
    ok(os.path.normcase(os.path.abspath(a_root)) not in roots_hit
       and os.path.normcase(os.path.abspath(b_root)) not in roots_hit,
       "D4b 两个合法域都没被列进待清清单", nd2)

    # D5 只读性
    ok(_nodes(cg) == n_before, "D5a 检测不改节点数", (_nodes(cg), n_before))
    ok(_ledger_bytes(cg) == led_before, "D5b 检测不改水位文件",
       (_ledger_bytes(cg), led_before))

    print("")
    if FAILS:
        print("issue #770 检测：失败 %d 项 -> %s" % (len(FAILS), FAILS))
        return 1
    print("issue #770 检测：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
