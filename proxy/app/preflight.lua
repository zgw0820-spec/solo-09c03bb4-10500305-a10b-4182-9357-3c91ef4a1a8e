-- 预检脚本：决定当前请求是否允许通过熔断器
--
-- 全局熔断器状态存放在 KEYS[1] 这个 hash 中，字段：
--   state       CLOSED / OPEN / HALF_OPEN
--   generation  状态机代数，每次状态切换（含每次发放新探测权）+1
--   failures    CLOSED 下的连续超时计数（两台代理合计）
--   opened_at   进入 OPEN 的毫秒时间戳
--   probe_owner 当前探测权的持有代理
--
-- 探测权（KEYS[1] .. ':probe'）是一份带 TTL 的全局租约：
--   * 一份租约只放行一条真实请求——持有代理自己的其它并发请求同样拒绝；
--   * 租约到期后，任意代理都可以接手，接手时 generation + 1，
--     使上一份探测（以及更早的在途请求）的结果全部过期；
--   * 只有当前 generation 的探测结果能推进状态机。
--
-- 入参：
--   ARGV[1] now_ms      代理当前毫秒时间戳
--   ARGV[2] cooldown_ms 冷却时长（毫秒）
--   ARGV[3] threshold   熔断阈值（连续超时次数）
--   ARGV[4] proxy_id    发起请求的代理标识
--   ARGV[5] probe_ttl_ms 探测权租约 TTL（毫秒），代理退出时自动释放
--
-- 返回：
--   { allow, state, generation, reason }
--   allow=1 放行；allow=0 快速拒绝
--   reason: OK / COOLDOWN / PROBE_IN_PROGRESS
local key = KEYS[1]
local probe_key = key .. ':probe'
local now = tonumber(ARGV[1])
local cooldown = tonumber(ARGV[2])
local threshold = tonumber(ARGV[3])
local proxy_id = ARGV[4]
local probe_ttl = tonumber(ARGV[5])

if redis.call('EXISTS', key) == 0 then
    -- 首次访问：初始化为 CLOSED
    redis.call('HSET', key, 'state', 'CLOSED', 'generation', 0,
                       'failures', 0, 'opened_at', 0, 'probe_owner', '')
end

local state = redis.call('HGET', key, 'state')
local generation = tonumber(redis.call('HGET', key, 'generation'))

if state == 'OPEN' then
    local opened_at = tonumber(redis.call('HGET', key, 'opened_at'))
    if now - opened_at < cooldown then
        -- 冷却中：快速拒绝
        return { 0, 'OPEN', generation, 'COOLDOWN' }
    end
    -- 冷却结束 -> 发放一份新的探测权，全局只放行这一条请求。
    -- generation +1 让上一代在途请求的结果全部失效。
    generation = generation + 1
    redis.call('HSET', key, 'state', 'HALF_OPEN',
                       'generation', generation,
                       'probe_owner', proxy_id)
    redis.call('SET', probe_key, proxy_id, 'PX', probe_ttl)
    return { 1, 'HALF_OPEN', generation, 'OK' }
end

if state == 'HALF_OPEN' then
    if redis.call('EXISTS', probe_key) == 1 then
        -- 探测租约仍有效：不论请求来自哪台代理（包括探测持有者自己），
        -- 都不能再放行第二条真实请求。
        return { 0, 'HALF_OPEN', generation, 'PROBE_IN_PROGRESS' }
    end
    -- 租约已过期（探测代理退出/停顿超期）：任意代理可接手。
    -- generation +1 后，旧探测迟到的成功或失败都无法再影响状态，
    -- 也不能清除本接手者刚拿到的探测权。
    generation = generation + 1
    redis.call('HSET', key, 'state', 'HALF_OPEN',
                       'generation', generation,
                       'probe_owner', proxy_id)
    redis.call('SET', probe_key, proxy_id, 'PX', probe_ttl)
    return { 1, 'HALF_OPEN', generation, 'OK' }
end

-- CLOSED：正常转发
return { 1, 'CLOSED', generation, 'OK' }
