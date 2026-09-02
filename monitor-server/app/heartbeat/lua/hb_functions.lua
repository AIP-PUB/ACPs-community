#!lua name=amp_heartbeat

-- AMP Heartbeat Redis Functions 库（6 个 Function）
-- 所有时间取 redis.call("TIME")（C-TIME-3）；阈值由 ARGV 传入（C-TIME-2）。
-- 文件头必须为 #!lua name=amp_heartbeat（FUNCTION LOAD 要求）。

-- ── 工具函数 ─────────────────────────────────────────────────────────────────

local function redis_now_ms()
    local t = redis.call("TIME")
    return tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
end

-- XADD helper：支持 outbox_max_len=0 省略 MAXLEN（§6.3 约定）
-- 注意：Redis 使用 Lua 5.1，全局 unpack 而非 table.unpack
local function xadd_outbox(outbox_key, max_len, fields)
    if tonumber(max_len) > 0 then
        return redis.call("XADD", outbox_key, "MAXLEN", "~", tostring(max_len), "*", unpack(fields))
    else
        return redis.call("XADD", outbox_key, "*", unpack(fields))
    end
end

-- ── hb_apply_heartbeat ────────────────────────────────────────────────────────
-- KEYS: [latest_key, liveness_zset_key, delta_seq_key, delta_outbox_key]
-- ARGV: [aic, observed_at_ms, observed_at_iso, source_ts_ms|"",
--        refresh_emit_interval_ms, outbox_max_len]
-- 返回: {status, kind|"", seq|0}

redis.register_function("hb_apply_heartbeat", function(keys, args)
    local latest_key   = keys[1]
    local zset_key     = keys[2]
    local seq_key      = keys[3]
    local outbox_key   = keys[4]

    local aic                     = args[1]
    local observed_at_ms          = tonumber(args[2])
    local observed_at_iso         = args[3]
    local source_ts_ms            = args[4]   -- 可能为 "" 表示 nil
    local refresh_emit_interval_ms = tonumber(args[5])
    local outbox_max_len          = tonumber(args[6])

    -- 步骤 1：HMGET 预读快照
    local prev_fields = redis.call("HMGET", latest_key,
        "last_seen_at_ms",
        "last_delta_seen_at_ms",
        "alive_membership_state",
        "last_delta_seq")
    local prev_last_seen_at_ms       = tonumber(prev_fields[1])
    local prev_last_delta_seen_at_ms = tonumber(prev_fields[2])
    local prev_membership            = prev_fields[3]
    local prev_last_delta_seq        = tonumber(prev_fields[4])

    -- 步骤 2：stale 短路（C-WRITE-2）
    if prev_last_seen_at_ms ~= nil and observed_at_ms <= prev_last_seen_at_ms then
        return {"ignored_older", "", 0}
    end

    -- 步骤 3：写入 hash 基本字段
    local hset_args = {
        latest_key,
        "last_seen_at_ms", tostring(observed_at_ms),
        "last_seen_at",    observed_at_iso,
        "alive_membership_state", "alive"
    }
    if source_ts_ms ~= nil and source_ts_ms ~= "" then
        table.insert(hset_args, "source_timestamp_ms")
        table.insert(hset_args, tostring(source_ts_ms))
    end
    redis.call("HSET", unpack(hset_args))

    -- 步骤 4：ZADD liveness_zset
    redis.call("ZADD", zset_key, tostring(observed_at_ms), aic)

    -- 步骤 5：判定 delta kind（使用步骤 1 的快照，C-WRITE-4）
    -- 注意：Redis Lua 中 HMGET 缺失字段返回 Lua false（非 nil），须用 not 判断
    local kind = nil
    if not prev_membership or prev_membership == "left_alive" then
        kind = "enter_alive"
    elseif prev_membership == "alive" then
        local delta_age = observed_at_ms - (prev_last_delta_seen_at_ms or 0)
        if prev_last_delta_seen_at_ms == nil or delta_age >= refresh_emit_interval_ms then
            kind = "refresh_alive"
        end
    end

    if kind == nil then
        -- 不产出 delta
        return {"applied", "", 0}
    end

    -- 步骤 6：命中时写 delta（原子）
    local seq = redis.call("INCR", seq_key)
    redis.call("HSET", latest_key,
        "last_delta_seen_at_ms", tostring(observed_at_ms),
        "last_delta_seq",        tostring(seq))

    local outbox_fields = {
        "seq",              tostring(seq),
        "kind",             kind,
        "op",               "upsert",
        "aic",              aic,
        "last_seen_at_ms",  tostring(observed_at_ms)
    }
    if source_ts_ms ~= nil and source_ts_ms ~= "" then
        table.insert(outbox_fields, "source_timestamp_ms")
        table.insert(outbox_fields, tostring(source_ts_ms))
    end

    xadd_outbox(outbox_key, outbox_max_len, outbox_fields)

    return {"applied_with_delta", kind, seq}
end)

-- ── hb_mark_silent_one ───────────────────────────────────────────────────────
-- KEYS: [latest_key, liveness_zset_key, delta_seq_key, delta_outbox_key]
-- ARGV: [aic, silence_threshold_ms, outbox_max_len]
-- 返回: {status, seq|0}
-- status ∈ {skipped_missing, skipped_membership, skipped_refreshed, left_alive}

redis.register_function("hb_mark_silent_one", function(keys, args)
    local latest_key  = keys[1]
    local zset_key    = keys[2]
    local seq_key     = keys[3]
    local outbox_key  = keys[4]

    local aic                 = args[1]
    local silence_threshold_ms = tonumber(args[2])
    local outbox_max_len      = tonumber(args[3])

    -- 步骤 1：预读 hash
    local fields = redis.call("HMGET", latest_key,
        "last_seen_at_ms",
        "alive_membership_state",
        "last_delta_seq")
    local last_seen_at_ms = tonumber(fields[1])
    local membership      = fields[2]
    local last_delta_seq  = fields[3]

    -- skip: Hash 缺失
    if last_seen_at_ms == nil then
        -- 清除可能存在的悬挂 zset 条目
        redis.call("ZREM", zset_key, aic)
        return {"skipped_missing", 0}
    end

    -- skip: membership 已为 left_alive
    if membership == "left_alive" then
        return {"skipped_membership", 0}
    end

    -- 取 Redis TIME 做时间判断（C-TIME-3）
    local now_ms = redis_now_ms()

    -- skip: 含界拒绝——last_seen_at_ms 在阈值内（silence_ms <= threshold，含界=alive）
    local silence_ms = now_ms - last_seen_at_ms
    if silence_ms <= silence_threshold_ms then
        return {"skipped_refreshed", 0}
    end

    -- 命中：原子提交 left_alive（C-WRITE-1）
    local seq = redis.call("INCR", seq_key)

    redis.call("HSET", latest_key,
        "alive_membership_state", "left_alive",
        "last_delta_seq",         tostring(seq))

    redis.call("ZREM", zset_key, aic)

    local outbox_fields = {
        "seq",   tostring(seq),
        "kind",  "leave_alive",
        "op",    "delete",
        "aic",   aic,
        "reason", "silent"
    }
    xadd_outbox(outbox_key, outbox_max_len, outbox_fields)

    return {"left_alive", seq}
end)

-- ── hb_evict_one ─────────────────────────────────────────────────────────────
-- KEYS: [latest_key, liveness_zset_key, delta_seq_key, delta_outbox_key]
-- ARGV: [aic, evict_after_ms, outbox_max_len]
-- 返回: {status, seq|0}
-- status ∈ {skipped_missing, skipped_refreshed, evicted, evicted_with_repair}

redis.register_function("hb_evict_one", function(keys, args)
    local latest_key  = keys[1]
    local zset_key    = keys[2]
    local seq_key     = keys[3]
    local outbox_key  = keys[4]

    local aic          = args[1]
    local evict_after_ms = tonumber(args[2])
    local outbox_max_len = tonumber(args[3])

    -- 预读 hash
    local fields = redis.call("HMGET", latest_key,
        "last_seen_at_ms",
        "alive_membership_state")
    local last_seen_at_ms = tonumber(fields[1])
    local membership      = fields[2]

    -- skip: Hash 缺失
    if last_seen_at_ms == nil then
        return {"skipped_missing", 0}
    end

    -- 取 Redis TIME
    local now_ms = redis_now_ms()
    local age_ms = now_ms - last_seen_at_ms

    -- skip: 未超出 evict 阈值
    if age_ms <= evict_after_ms then
        return {"skipped_refreshed", 0}
    end

    -- left_alive：直接 DEL+ZREM（不产出 delta）
    if membership == "left_alive" then
        redis.call("DEL", latest_key)
        redis.call("ZREM", zset_key, aic)
        return {"evicted", 0}
    end

    -- alive：原子 repair + DEL + ZREM（C-WRITE-3）
    local seq = redis.call("INCR", seq_key)

    local outbox_fields = {
        "seq",    tostring(seq),
        "kind",   "leave_alive",
        "op",     "delete",
        "aic",    aic,
        "reason", "evict_repair"
    }
    xadd_outbox(outbox_key, outbox_max_len, outbox_fields)

    redis.call("DEL", latest_key)
    redis.call("ZREM", zset_key, aic)

    return {"evicted_with_repair", seq}
end)

-- ── hb_relay_commit ──────────────────────────────────────────────────────────
-- KEYS: [published_seq_key, relay_epoch_key]
-- ARGV: [epoch, seq]
-- 返回: 1 成功 | 0 epoch 过期（fencing，C-RELAY-1）

redis.register_function("hb_relay_commit", function(keys, args)
    local published_seq_key = keys[1]
    local relay_epoch_key   = keys[2]

    local epoch = tostring(args[1])
    local seq   = tostring(args[2])

    local current_epoch = redis.call("GET", relay_epoch_key)
    if not current_epoch or tostring(current_epoch) ~= epoch then
        return 0
    end

    redis.call("SET", published_seq_key, seq)
    return 1
end)

-- ── hb_relay_ack ─────────────────────────────────────────────────────────────
-- KEYS: [delta_outbox_key, relay_epoch_key]
-- ARGV: [epoch, entry_id, group]
-- 返回: 1 成功 | 0 epoch 过期（防止旧 owner XACK 消除新 owner PEL 条目，C-RELAY-1）

redis.register_function("hb_relay_ack", function(keys, args)
    local outbox_key      = keys[1]
    local relay_epoch_key = keys[2]

    local epoch    = tostring(args[1])
    local entry_id = args[2]
    local group    = args[3]

    local current_epoch = redis.call("GET", relay_epoch_key)
    if not current_epoch or tostring(current_epoch) ~= epoch then
        return 0
    end

    redis.call("XACK", outbox_key, group, entry_id)
    return 1
end)

-- ── hb_relay_trim ─────────────────────────────────────────────────────────────
-- KEYS: [delta_outbox_key, relay_epoch_key]
-- ARGV: [epoch, min_entry_id]
-- 返回: 1 成功 | 0 epoch 过期（防止僵尸 relay 裁掉新 owner 待 republish 的 PEL 条目，C-RELAY-1）

redis.register_function("hb_relay_trim", function(keys, args)
    local outbox_key      = keys[1]
    local relay_epoch_key = keys[2]

    local epoch        = tostring(args[1])
    local min_entry_id = args[2]

    local current_epoch = redis.call("GET", relay_epoch_key)
    if not current_epoch or tostring(current_epoch) ~= epoch then
        return 0
    end

    redis.call("XTRIM", outbox_key, "MINID", min_entry_id)
    return 1
end)
