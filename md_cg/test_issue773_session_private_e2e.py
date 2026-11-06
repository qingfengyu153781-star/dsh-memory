# -*- coding: utf-8 -*-
"""#773 端到端夹具：sessionPrivate 四条读路径隔离（服务端落盘 → 跨会话读不到）。

背景（issue #773）
------------------
#772 的守卫只覆盖「插件侧把 sensitivity 放进 remember 的 extra」，
**没有任何断言证明服务端落盘的 frontmatter 真的是 private、且别的会话真的读不到**。
代码链是 client 展 extra → `mcp_server`（本文件被测行 4389）透传
→ `remember_gated` 的 `**kw` → `add` → frontmatter。任一跳丢键都会**静默失效**，
而现有测试全绿——这正是本夹具要钉死的东西。

本夹具与 #772 的分工
--------------------
· #772：插件侧请求体里有没有 `sensitivity` 这个键（进程外，单元级）。
· 本文件：**真 MdCGSecure + 临时根 + master_key（KEK）+ 两个不同 session 的 principal**，
  真走 `call_tool("mdcg_remember")` 落盘，再从**四条读路径**逐条断言另一个会话读不到。
  每条读路径一个独立断言、各自有名字（t2/t3/t4/t5），便于定点排查。

改动点与被测行
--------------
落盘透传点：`md_cg/mcp_server.py` 的 `mdcg_remember` → `cg.remember_gated(...)` 调用里的
`sensitivity=a.get("sensitivity")`（本仓库 main 3619402b 实测在 **:4389**；
任务卡写的 4314 是旧行号，已随 209 个提交漂移）。删掉该形参即「服务端静默丢密级」，
本夹具必须点红（见卡片完成判据 b）。

判据（fail-closed 口径）
------------------------
· 私密节点跨会话读不到 ⇒ 返回 None / 不出现在 pack / recent / timeline；
· **不是**「读到了但内容为空」——那会把隔离退化成不可区分；
· 反向断言：写者自己的会话必须能读到（防「隔离退化为不可读」）。

运行
----
    python -m md_cg.test_issue773_session_private_e2e      # 自跑，退出码即结论
    python -m pytest -q md_cg/test_issue773_session_private_e2e.py   # 亦可
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

from . import mcp_server as ms
from .mdcos import MdCGSecure, UNATTRIBUTED_SESSION
from .security import Principal

PASS = 0
FAIL = 0
FAILS = []

SESSION_A = "sA-773"
SESSION_B = "sB-773"
NID = "n773priv"
SECRET = "773-私密载荷-仅A会话可见-9f2c"


def ok(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        FAILS.append(label)
        print("  FAIL %s" % label)


def _principal(actor, session, clearance="secret"):
    return Principal(tenant="t1", actor=actor, clearance=clearance,
                     can_write=True, can_admin=True, session=session)


def _open(root, kek, actor, session, clearance="secret"):
    return MdCGSecure(root, principal=_principal(actor, session, clearance),
                      master_key=kek)


def _call(cg, name, args):
    try:
        return ms.call_tool(cg, name, args)
    except Exception as exc:                       # noqa: BLE001 - 夹具要看清失败面
        return {"_exception": "%s: %s" % (type(exc).__name__, exc)}


def _frontmatter_fields(cg, node_id):
    """读**落盘** frontmatter（不经读面，避免读面自身掩盖落盘问题）。"""
    from . import nodefile
    rel = cg.index["nodes"][node_id]["path"]
    with open(os.path.join(cg.root, rel), encoding="utf-8") as f:
        fm, _content = nodefile.loads(f.read())
    return fm


def _pack_ids(recall_result):
    out = []
    for row in (recall_result or {}).get("pack") or []:
        out.append(row.get("id") or row.get("node_id") if isinstance(row, dict) else row)
    return out


def _recent_ids(recall_result):
    out = []
    for row in (recall_result or {}).get("recent") or []:
        out.append(row.get("id") or row.get("node_id") if isinstance(row, dict) else row)
    return out


def _timeline_ids(stg_result):
    if not isinstance(stg_result, dict):
        return []
    rows = stg_result.get("items") or stg_result.get("entries") or stg_result.get("timeline") or []
    out = []
    for row in rows:
        out.append(row.get("id") or row.get("node_id") if isinstance(row, dict) else row)
    return out


def main():
    tmp = tempfile.mkdtemp(prefix="mdcg_773_")
    kek = os.urandom(32)
    try:
        # ── 夹具：写者 A（session=sA-773，private 密级） ──────────────────
        cgA = _open(tmp, kek, "alice", SESSION_A, clearance="private")
        w = _call(cgA, "mdcg_remember", {
            "node_id": NID, "content": SECRET, "layer": "contextual",
            "sensitivity": "private", "gated": True,
            "importance_hint": 0.95, "override": True,
        })
        committed = bool(isinstance(w, dict) and w.get("ok") and (w.get("written") or w.get("node_id")))

        # ── t1：落盘 frontmatter 真的是 private（服务端没静默降级） ───────
        fm = {}
        if NID in getattr(cgA, "index", {}).get("nodes", {}):
            fm = _frontmatter_fields(cgA, NID)
        ok(committed, "t1a写入 committed（gated 分支真的落盘了；返回=%r）"
                     % (w if not committed else "ok"))
        ok(fm.get("sensitivity") == "private",
           "t1b**落盘 frontmatter sensitivity == 'private'**（实得 %r；"
           "丢键 ⇒ 这条先红）" % fm.get("sensitivity"))
        ok(fm.get("session") == SESSION_A,
           "t1c落盘 frontmatter session == %r（实得 %r）" % (SESSION_A, fm.get("session")))

        # ── 读面：B（不同 session、不同 actor、同 tenant、同 KEK） ─────────
        cgB = _open(tmp, kek, "bob", SESSION_B, clearance="private")

        # t2：get
        gotB = _call(cgB, "mdcg_get", {"node_id": NID})
        payloadB = gotB.get("content") if isinstance(gotB, dict) else None
        ok(payloadB is None or SECRET not in str(payloadB),
           "t2【读路径·get】B 会话读不到私密内容（实得 %r）" % (str(payloadB)[:40],))

        # t3：recall 正排（pack）
        rB = _call(cgB, "mdcg_recall", {"query": SECRET, "k": 20})
        ok(NID not in _pack_ids(rB),
           "t3【读路径·recall 正排】B 的 pack 不含该 id（实得 %r）" % (_pack_ids(rB)[:5],))

        # t4：recall 的 recent 段
        ok(NID not in _recent_ids(rB),
           "t4【读路径·recall recent 段】B 的 recent 不含该 id（实得 %r）"
           % (_recent_ids(rB)[:5],))

        # t5：timeline（autoRecall 前置）
        tB = _call(cgB, "stg", {"op": "timeline", "limit": 50,
                                "session": SESSION_B})
        ok(NID not in _timeline_ids(tB),
           "t5【读路径·timeline】B 的会话视图不含该 id（实得 %r；返回键=%r）"
           % (_timeline_ids(tB)[:5], sorted(tB.keys()) if isinstance(tB, dict) else tB))

        # t6：反向断言——写者 A 自己必须读得到（防隔离退化为不可读）
        gotA = _call(cgA, "mdcg_get", {"node_id": NID})
        ok(isinstance(gotA, dict) and SECRET in str(gotA.get("content") or ""),
           "t6【反向】A 会话读得到自己写的私密内容")
        tA = _call(cgA, "stg", {"op": "timeline", "limit": 50, "session": SESSION_A})
        ok(NID in _timeline_ids(tA),
           "t6b【反向】A 的 timeline 会话视图含该 id（实得 %r，共 %d 条）"
           % (_timeline_ids(tA)[:5], len(_timeline_ids(tA))))

        # ── t7：无 KEK ⇒ fail-closed（不许静默降级成 internal 落盘） ──────
        tmp2 = tempfile.mkdtemp(prefix="mdcg_773_nokek_")
        try:
            cgN = MdCGSecure(tmp2, principal=_principal("alice", SESSION_A,
                                                        clearance="private"))
            rN = _call(cgN, "mdcg_remember", {
                "node_id": "n773nokek", "content": SECRET, "layer": "contextual",
                "sensitivity": "private", "gated": True,
                "importance_hint": 0.95, "override": True,
            })
            threw = "_exception" in rN
            committedN = bool(rN.get("committed")) if isinstance(rN, dict) else False
            fmN = {}
            if "n773nokek" in getattr(cgN, "index", {}).get("nodes", {}):
                fmN = _frontmatter_fields(cgN, "n773nokek")
            # fail-closed 判据：要么抛错、要么拒写、要么落盘仍标 private；
            # 三者之外 = 静默降级成 internal（本断言要红的正是这一种）。
            silent_downgrade = (committedN and not threw
                                and fmN.get("sensitivity") != "private")
            ok(not silent_downgrade,
               "t7无 KEK 写 private ⇒ fail-closed（抛错=%s / committed=%s / "
               "落盘密级=%r / gate=%r）"
               % (threw, committedN, fmN.get("sensitivity"),
                  (rN or {}).get("gate") if isinstance(rN, dict) else None))
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

        # ── t8：'unassigned' 哨兵 + 读门绑定面（实测口径，勿想当然） ──────
        # 实测（本仓 main 3619402b，probe 见 REPORT）：private 节点的读门绑
        # **(tenant, actor)**，**不绑 session**——同 actor 换 session 仍可读。
        # 故「两枚不同 session 的 principal」能隔离，是因为它们同时换了 actor；
        # 单换 session 不是隔离边界。这条断言把该事实钉住，防两件事：
        #   ①有人把 session 误当读门（改坏 actor 隔离却仍全绿）；
        #   ②有人把 actor 隔离放松成 session 隔离。
        cgSameActor = _open(tmp, kek, "alice", "unassigned", clearance="private")
        sameActorGot = _call(cgSameActor, "mdcg_get", {"node_id": NID})
        ok(str((sameActorGot or {}).get("content") or "").find(SECRET) >= 0,
           "t8【实测口径】同 actor 换 session（'unassigned'）**仍可读**——"
           "private 读门绑 (tenant,actor)，session 只是归因标签")
        cgOtherUnassigned = _open(tmp, kek, "carol", "unassigned", clearance="private")
        otherGot = _call(cgOtherUnassigned, "mdcg_get", {"node_id": NID})
        ok((otherGot or {}).get("content") is None,
           "t8b' unassigned' 会话 + 不同 actor ⇒ 读不到（未被当作通配桶）")
        ok(UNATTRIBUTED_SESSION == "unattributed",
           "t8c服务端写归因哨兵仍是 %r（与插件侧 'unassigned' 显式并存、未统一）"
           % (UNATTRIBUTED_SESSION,))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nPASS=%d FAIL=%d" % (PASS, FAIL))
    if FAIL:
        print("FAILS:\n  " + "\n  ".join(FAILS))
    return 1 if FAIL else 0


def test_issue773_session_private_e2e():
    """pytest 入口：同一组断言（失败时 raise）。"""
    rc = main()
    assert rc == 0, "%d 条断言失败" % FAIL


if __name__ == "__main__":
    sys.exit(main())
