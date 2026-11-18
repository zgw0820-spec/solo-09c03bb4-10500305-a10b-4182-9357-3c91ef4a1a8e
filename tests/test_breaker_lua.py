"""Lua 熔断器状态机的确定性单元测试。

直接用真实 Redis（REDIS_URL 环境变量，默认本地 6379）执行 Lua 脚本，
逐条验证题目中的状态机语义，包括普通单元测试里难以稳定复现的
“旧请求迟到失败不能推翻新一轮成功”。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from redis import Redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
LUA_DIR = Path(__file__).resolve().parents[1] / "proxy" / "app"

KEY = "test:cb:state"
PROBE = KEY + ":probe"
THRESHOLD = 3
COOLDOWN_MS = 5_000
PROBE_TTL_MS = 10_000


@pytest.fixture()
def r():
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    client.ping()
    client.delete(KEY, PROBE)
    yield client
    client.delete(KEY, PROBE)


@pytest.fixture()
def scripts(r):
    pre = r.register_script((LUA_DIR / "preflight.lua").read_text(encoding="utf-8"))
    rep = r.register_script((LUA_DIR / "report_result.lua").read_text(encoding="utf-8"))
    return pre, rep


def pre(pre_script, r, proxy="proxy-a", now=None, ttl=PROBE_TTL_MS):
    now = now or int(time.time() * 1000)
    allowed, state, gen, reason = pre_script(
        keys=[KEY], args=[now, COOLDOWN_MS, THRESHOLD, proxy, ttl]
    )
    return {"allowed": bool(allowed), "state": state, "generation": int(gen), "reason": reason}


def trip_open(pre_script, rep_script, r, proxy="proxy-a"):
    """三次超时让全局熔断器进入 OPEN，返回 opened_at 与 OPEN 代。"""
    for _ in range(THRESHOLD):
        d = pre(pre_script, r, proxy)
        report(rep_script, r, d["generation"], False, proxy)
    assert r.hget(KEY, "state") == "OPEN"
    return int(r.hget(KEY, "opened_at")), int(r.hget(KEY, "generation"))


def report(rep_script, r, gen, success, proxy="proxy-a", now=None):
    now = now or int(time.time() * 1000)
    applied, state, new_gen, failures = rep_script(
        keys=[KEY], args=[now, THRESHOLD, COOLDOWN_MS, proxy, gen, 1 if success else 0]
    )
    return {"applied": bool(applied), "state": state,
            "generation": int(new_gen), "failures": int(failures)}


def test_closed_allows_and_success_resets_failures(r, scripts):
    pre_script, rep_script = scripts
    d1 = pre(pre_script, r)
    assert d1["allowed"] and d1["state"] == "CLOSED"
    report(rep_script, r, d1["generation"], False)
    report(rep_script, r, d1["generation"], False)
    assert int(r.hget(KEY, "failures")) == 2
    d2 = pre(pre_script, r)
    report(rep_script, r, d2["generation"], True)
    assert r.hget(KEY, "state") == "CLOSED"
    assert int(r.hget(KEY, "failures")) == 0


def test_three_failures_across_two_proxies_opens_breaker(r, scripts):
    pre_script, rep_script = scripts
    # 失败分散在两台代理上，合计三次即熔断
    for i, proxy in enumerate(["proxy-a", "proxy-b", "proxy-a"]):
        d = pre(pre_script, r, proxy)
        out = report(rep_script, r, d["generation"], False, proxy)
        if i < 2:
            assert out["state"] == "CLOSED"
        else:
            assert out["state"] == "OPEN"
    # 冷却期内：快速拒绝，且不放任何探测
    for proxy in ("proxy-a", "proxy-b"):
        d = pre(pre_script, r, proxy)
        assert not d["allowed"] and d["reason"] == "COOLDOWN"
    assert r.hget(KEY, "state") == "OPEN"


def test_cooldown_then_single_probe_failure_reopens_cooldown(r, scripts):
    pre_script, rep_script = scripts
    for _ in range(THRESHOLD):
        d = pre(pre_script, r)
        report(rep_script, r, d["generation"], False)
    opened_at = int(r.hget(KEY, "opened_at"))

    # 冷却未结束：拒绝
    d = pre(pre_script, r, "proxy-a", now=opened_at + 4_999)
    assert not d["allowed"] and d["reason"] == "COOLDOWN"

    # 冷却结束：只放出一个探测（proxy-a 拿到）
    d_a = pre(pre_script, r, "proxy-a", now=opened_at + 5_000)
    assert d_a["allowed"] and d_a["state"] == "HALF_OPEN"
    probe_gen = d_a["generation"]
    assert r.get(PROBE) == "proxy-a"

    # proxy-b 的请求被快速拒绝：全局只有一个探测
    d_b = pre(pre_script, r, "proxy-b", now=opened_at + 5_001)
    assert not d_b["allowed"] and d_b["reason"] == "PROBE_IN_PROGRESS"

    # 探测失败：重新冷却，且代数 +1
    out = report(rep_script, r, probe_gen, False, "proxy-a", now=opened_at + 5_100)
    assert out["state"] == "OPEN"
    assert out["generation"] == probe_gen + 1
    assert r.get(PROBE) is None  # 探测租约被清理
    # 新冷却期内仍快速拒绝
    d = pre(pre_script, r, "proxy-b", now=opened_at + 5_200)
    assert not d["allowed"] and d["reason"] == "COOLDOWN"


def test_probe_success_recovers_and_clears_failures(r, scripts):
    pre_script, rep_script = scripts
    for _ in range(THRESHOLD):
        d = pre(pre_script, r)
        report(rep_script, r, d["generation"], False)
    opened_at = int(r.hget(KEY, "opened_at"))

    d = pre(pre_script, r, "proxy-b", now=opened_at + 5_000)
    assert d["state"] == "HALF_OPEN"
    out = report(rep_script, r, d["generation"], True, "proxy-b", now=opened_at + 5_050)
    assert out["state"] == "CLOSED"
    assert out["failures"] == 0
    assert r.get(PROBE) is None
    # 恢复后重新放行
    again = pre(pre_script, r, "proxy-a", now=opened_at + 6_000)
    assert again["allowed"] and again["state"] == "CLOSED"


def test_stale_failure_cannot_overturn_new_round_success(r, scripts):
    """关键语义：上一代迟到的失败结果，不能推翻新一轮探测成功。"""
    pre_script, rep_script = scripts
    # 1) 三次失败 -> OPEN（generation 变为 1）
    gens = []
    for _ in range(THRESHOLD):
        d = pre(pre_script, r, "proxy-a")
        gens.append(d["generation"])
        report(rep_script, r, d["generation"], False, "proxy-a")
    opened_at = int(r.hget(KEY, "opened_at"))
    gen_open = int(r.hget(KEY, "generation"))

    # 2) 冷却结束，proxy-a 拿到探测（generation 变为 2）
    probe = pre(pre_script, r, "proxy-a", now=opened_at + 5_000)
    assert probe["state"] == "HALF_OPEN"
    gen_half = probe["generation"]
    assert gen_half == gen_open + 1

    # 3) 探测成功 -> CLOSED（generation 变为 3）
    ok = report(rep_script, r, gen_half, True, "proxy-a", now=opened_at + 5_050)
    assert ok["state"] == "CLOSED"
    gen_closed = int(r.hget(KEY, "generation"))

    # 4) 旧代请求（OPEN 代、HALF_OPEN 代）的迟到失败陆续回来：必须全部忽略
    stale1 = report(rep_script, r, gen_open, False, "proxy-a", now=opened_at + 9_000)
    stale2 = report(rep_script, r, gen_half, False, "proxy-b", now=opened_at + 9_100)
    assert stale1["applied"] is False
    assert stale2["applied"] is False
    assert r.hget(KEY, "state") == "CLOSED"
    assert int(r.hget(KEY, "failures")) == 0
    assert int(r.hget(KEY, "generation")) == gen_closed

    # 5) 新一轮的真实失败才被计数
    d = pre(pre_script, r, "proxy-b", now=opened_at + 10_000)
    fresh = report(rep_script, r, d["generation"], False, "proxy-b", now=opened_at + 10_100)
    assert fresh["applied"] is True
    assert fresh["failures"] == 1


def test_probe_lease_expiry_allows_recovery_after_proxy_exit(r, scripts):
    """代理在探测途中退出：租约到期后，另一个代理仍能拿到探测权继续恢复。"""
    pre_script, rep_script = scripts
    for _ in range(THRESHOLD):
        d = pre(pre_script, r)
        report(rep_script, r, d["generation"], False)
    opened_at = int(r.hget(KEY, "opened_at"))

    d = pre(pre_script, r, "proxy-a", now=opened_at + 5_000)
    assert d["state"] == "HALF_OPEN"
    # proxy-a 崩溃，没有上报任何结果；等待租约过期
    r.delete(PROBE)  # 模拟 TTL 到期
    again = pre(pre_script, r, "proxy-b", now=opened_at + 20_000)
    assert again["allowed"] and again["state"] == "HALF_OPEN"
    out = report(rep_script, r, again["generation"], True, "proxy-b", now=opened_at + 20_100)
    assert out["state"] == "CLOSED"


def test_same_proxy_concurrent_requests_get_only_one_probe(r, scripts):
    """同一份探测权只放行一条真实请求：探测持有者自己的并发请求也要拒绝。"""
    pre_script, rep_script = scripts
    opened_at, _ = trip_open(pre_script, rep_script, r)

    first = pre(pre_script, r, "proxy-a", now=opened_at + 5_000)
    assert first["allowed"] and first["state"] == "HALF_OPEN"
    assert r.get(PROBE) == "proxy-a"

    # 同一代理的第二个、第三个并发请求（探测仍在途）全部快速拒绝
    for delta in (1, 2, 3):
        other = pre(pre_script, r, "proxy-a", now=opened_at + 5_000 + delta)
        assert not other["allowed"], "同一代理的并发请求不应再获得探测权"
        assert other["reason"] == "PROBE_IN_PROGRESS"
        assert other["generation"] == first["generation"]

    # 另一个代理同样拒绝
    peer = pre(pre_script, r, "proxy-b", now=opened_at + 5_010)
    assert not peer["allowed"] and peer["reason"] == "PROBE_IN_PROGRESS"

    # 只有最初的探测结果能推进状态机
    out = report(rep_script, r, first["generation"], True, "proxy-a",
                 now=opened_at + 5_100)
    assert out["applied"] is True and out["state"] == "CLOSED"


def test_stale_probe_success_cannot_clear_takeover_probe_lease(r, scripts):
    """关键缺陷回归：旧探测停顿超过租约、另一代理接手后，旧探测的成功结果
    既不能提前恢复转发，也不能清除接手者刚拿到的探测权。"""
    pre_script, rep_script = scripts
    opened_at, _ = trip_open(pre_script, rep_script, r)

    # proxy-a 抢到探测权，但在上报结果前停顿超过租约期限
    old = pre(pre_script, r, "proxy-a", now=opened_at + 5_000)
    assert old["allowed"] and old["state"] == "HALF_OPEN"

    # 租约自然过期（停顿超过 PROBE_TTL_MS，这里直接让 key 过期）
    r.delete(PROBE)
    assert r.get(PROBE) is None

    # proxy-b 接手：拿到新的探测权，代数必须前进，旧探测因此失效
    takeover = pre(pre_script, r, "proxy-b", now=opened_at + 5_000 + PROBE_TTL_MS + 100)
    assert takeover["allowed"] and takeover["state"] == "HALF_OPEN"
    assert takeover["generation"] > old["generation"]
    assert r.get(PROBE) == "proxy-b"

    # 旧探测迟到的成功结果：必须被忽略
    stale_ok = report(rep_script, r, old["generation"], True, "proxy-a",
                      now=opened_at + 5_000 + PROBE_TTL_MS + 200)
    assert stale_ok["applied"] is False
    assert r.hget(KEY, "state") == "HALF_OPEN"
    assert r.get(PROBE) == "proxy-b", "旧结果不得清除接手者的探测权"
    assert r.hget(KEY, "probe_owner") == "proxy-b"

    # 旧探测迟到的失败结果同样无效
    stale_fail = report(rep_script, r, old["generation"], False, "proxy-a",
                        now=opened_at + 5_000 + PROBE_TTL_MS + 300)
    assert stale_fail["applied"] is False
    assert r.hget(KEY, "state") == "HALF_OPEN"

    # 期间其它请求仍被拒绝（同一份有效探测权只放行一条真实请求）
    blocked = pre(pre_script, r, "proxy-a",
                  now=opened_at + 5_000 + PROBE_TTL_MS + 400)
    assert not blocked["allowed"] and blocked["reason"] == "PROBE_IN_PROGRESS"

    # 只有接手者的结果能决定恢复
    ok = report(rep_script, r, takeover["generation"], True, "proxy-b",
                now=opened_at + 5_000 + PROBE_TTL_MS + 500)
    assert ok["applied"] is True and ok["state"] == "CLOSED"


def test_stale_takeover_failure_reopens_cooldown(r, scripts):
    """接手探测失败时重新冷却；旧探测迟到的成功不能把它改回 CLOSED。"""
    pre_script, rep_script = scripts
    opened_at, _ = trip_open(pre_script, rep_script, r)

    old = pre(pre_script, r, "proxy-a", now=opened_at + 5_000)
    r.delete(PROBE)  # 租约过期
    takeover = pre(pre_script, r, "proxy-b", now=opened_at + 5_000 + PROBE_TTL_MS + 100)
    assert takeover["generation"] > old["generation"]

    fail = report(rep_script, r, takeover["generation"], False, "proxy-b",
                  now=opened_at + 5_000 + PROBE_TTL_MS + 200)
    assert fail["applied"] is True and fail["state"] == "OPEN"
    assert r.get(PROBE) is None

    stale_ok = report(rep_script, r, old["generation"], True, "proxy-a",
                      now=opened_at + 5_000 + PROBE_TTL_MS + 300)
    assert stale_ok["applied"] is False
    assert r.hget(KEY, "state") == "OPEN"
    # 新冷却期内仍快速拒绝
    d = pre(pre_script, r, "proxy-a", now=opened_at + 5_000 + PROBE_TTL_MS + 400)
    assert not d["allowed"] and d["reason"] == "COOLDOWN"


def test_probe_result_from_non_owner_is_ignored(r, scripts):
    """半开期只有探测持有者的上报有效；伪造/其它代理的结果无效。"""
    pre_script, rep_script = scripts
    opened_at, _ = trip_open(pre_script, rep_script, r)

    probe = pre(pre_script, r, "proxy-a", now=opened_at + 5_000)
    assert probe["allowed"]
    # 同代其它代理（未被放行）不能借相同代数上报成功
    forged = report(rep_script, r, probe["generation"], True, "proxy-b",
                    now=opened_at + 5_050)
    assert forged["applied"] is False
    assert r.hget(KEY, "state") == "HALF_OPEN"
    assert r.get(PROBE) == "proxy-a"

    ok = report(rep_script, r, probe["generation"], True, "proxy-a",
                now=opened_at + 5_100)
    assert ok["applied"] is True and ok["state"] == "CLOSED"


def test_probe_lease_has_ttl_and_expires(r, scripts):
    """探测权必须是带 TTL 的租约：到期后自动释放。"""
    pre_script, rep_script = scripts
    opened_at, _ = trip_open(pre_script, rep_script, r)
    d = pre(pre_script, r, "proxy-a", now=opened_at + 5_000, ttl=300)
    assert d["allowed"]
    ttl = r.pttl(PROBE)
    assert 0 < ttl <= 300
    time.sleep(0.45)
    assert r.get(PROBE) is None
    # 到期后另一个代理可接手
    again = pre(pre_script, r, "proxy-b", now=opened_at + 5_000 + 500)
    assert again["allowed"] and again["generation"] > d["generation"]


def test_redis_state_persistence_simulation(r, scripts):
    """模拟 Redis 重启：键被 AOF 恢复后，OPEN 状态与 opened_at 原样保留。

    真实重启验证见 tests/self_test.sh（docker compose restart redis）。
    """
    pre_script, rep_script = scripts
    for _ in range(THRESHOLD):
        d = pre(pre_script, r)
        report(rep_script, r, d["generation"], False)
    snapshot = r.hgetall(KEY)
    # 模拟重启：新建客户端（等价新连接），状态仍在
    r2 = Redis.from_url(REDIS_URL, decode_responses=True)
    assert r2.hgetall(KEY) == snapshot
    assert r2.hget(KEY, "state") == "OPEN"
