"""Candidate order for the personal `following` topic, weighted by the reader profile.

Pure functions; design: docs/design-anti-info-gap.md §3.4. Without a profile the caller
keeps the shared publisher interleave unchanged. With one:

1. Board quotas: each board with candidates first gets min(3, its candidates); the rest
   goes by W_board × candidates with the largest-remainder method (capped by supply).
2. Inside a board, sources are interleaved by smooth weighted round-robin on W_source;
   each source yields its items newest first (the order they arrive in).
3. Exploration: at least ceil(20 %) of the slots go to cold sources (decayed n+k+s < 5)
   when there are enough of them, and every source pushed down to the 0.25 floor keeps at
   least one slot, so its statistics can recover. Swaps replace the last pick of a
   well-rated source, preferably in the same board.
4. Boards are merged by smooth weighted round-robin on their quotas; that is the model order.
"""
import math

from core.feedback.boards import board_of
from core.feedback.profile import MIN_EFFECTIVE, W_MIN

BOARD_FLOOR = 3
EXPLORE_SHARE = 0.2


def apportion(total, weights, caps):
    """Largest-remainder split of `total` by `weights`, never above `caps` (dicts keyed alike)."""
    alloc = dict.fromkeys(weights, 0)
    while total > 0:
        open_ = [key for key in weights if alloc[key] < caps[key] and weights[key] > 0]
        if not open_:
            break
        mass = sum(weights[key] for key in open_)
        exact = {key: total * weights[key] / mass for key in open_}
        give = {key: min(int(exact[key]), caps[key] - alloc[key]) for key in open_}
        given = sum(give.values())
        for key in sorted(open_, key=lambda k: (-(exact[k] - int(exact[k])), -weights[k], str(k))):
            if given >= total:
                break
            if alloc[key] + give[key] < caps[key]:
                give[key] += 1
                given += 1
        for key in open_:
            alloc[key] += give[key]
        total -= given
    return alloc


def smooth_round_robin(queues, weights, limit):
    """Nginx-style smooth weighted round-robin over `queues` ({key: list}); ties keep key order."""
    queues = {key: list(items) for key, items in queues.items() if items}
    current = dict.fromkeys(queues, 0.0)
    result = []
    while queues and len(result) < limit:
        active = list(queues)
        total = sum(weights[key] for key in active)
        for key in active:
            current[key] += weights[key]
        pick = max(active, key=lambda key: current[key])  # max() keeps the first on ties
        current[pick] -= total
        result.append(queues[pick].pop(0))
        if not queues[pick]:
            del queues[pick]
            del current[pick]
    return result


def _board_quotas(counts, weights, limit):
    boards = list(counts)
    base = {board: min(BOARD_FLOOR, counts[board]) for board in boards}
    if sum(base.values()) > limit:
        # Too few slots for every floor: one each, heaviest boards first, until full.
        quotas, left = dict.fromkeys(boards, 0), limit
        order = sorted(boards, key=lambda b: (-weights[b], boards.index(b)))
        while left > 0:
            for board in order:
                if left and quotas[board] < base[board]:
                    quotas[board] += 1
                    left -= 1
        return quotas
    extra = apportion(limit - sum(base.values()), {b: weights[b] * counts[b] for b in boards},
                      {b: counts[b] - base[b] for b in boards})
    return {board: base[board] + extra[board] for board in boards}


def rank(candidates, profile, limit):
    """At most `limit` candidates in model order. `candidates` carry `publisher` and
    `source` (the section) and arrive newest first per publisher."""
    if not candidates or limit <= 0:
        return []
    limit = min(limit, len(candidates))
    boards, info = {}, {}
    for candidate in candidates:
        publisher = candidate["publisher"]
        board = board_of(publisher, candidate.get("source"))
        boards.setdefault(board, {}).setdefault(publisher, []).append(candidate)
        if publisher not in info:
            stat = profile.feature("source", publisher)
            weight = profile.weight_for_source(publisher)
            info[publisher] = {"weight": weight, "cold": stat is None or stat.effective < MIN_EFFECTIVE,
                               "floor": stat is not None and weight <= W_MIN + 1e-9}
    counts = {board: sum(len(items) for items in per.values()) for board, per in boards.items()}
    quotas = _board_quotas(counts, {b: profile.weight_for_board(b) for b in boards}, limit)
    chosen, spare = {}, {}
    for board, per in boards.items():
        order = smooth_round_robin(per, {p: info[p]["weight"] for p in per}, counts[board])
        chosen[board], spare[board] = order[:quotas[board]], order[quotas[board]:]
    _explore(chosen, spare, info, limit)
    arrival = {id(candidate): index for index, candidate in enumerate(candidates)}
    for board, picked in chosen.items():
        # Swaps may land out of order: each source's slots are refilled newest first.
        slots = {}
        for item in picked:
            slots.setdefault(item["publisher"], []).append(item)
        queues = {p: sorted(items, key=lambda c: arrival[id(c)]) for p, items in slots.items()}
        chosen[board] = [queues[item["publisher"]].pop(0) for item in picked]
    merged = smooth_round_robin(chosen, {b: max(len(items), 1) for b, items in chosen.items()}, limit)
    return merged


def _explore(chosen, spare, info, limit):
    """Swap in cold and floor-weight sources in place (§3.4 step 3)."""
    protected = set()

    def swappable(board):
        """Index of the pick to give up: the last one of a well-rated source with several
        picks, else the last well-rated pick at all; None when only explorers are left."""
        picked = chosen[board]
        per_source = {}
        for item in picked:
            per_source[item["publisher"]] = per_source.get(item["publisher"], 0) + 1
        fallback = None
        for index in range(len(picked) - 1, -1, -1):
            item = picked[index]
            meta = info[item["publisher"]]
            if id(item) in protected or meta["cold"] or meta["floor"]:
                continue
            if per_source[item["publisher"]] >= 2:
                return index
            if fallback is None:
                fallback = index
        return fallback

    def swap_in(board, item):
        index = swappable(board)
        donor = board
        if index is None:
            options = [(b, i) for b in chosen if (i := swappable(b)) is not None]
            if not options:
                return False
            donor, index = max(options, key=lambda pair: (len(chosen[pair[0]]), str(pair[0])))
        removed = chosen[donor].pop(index)
        spare[donor].insert(0, removed)
        spare[board].remove(item)
        if donor == board:
            chosen[board].insert(index, item)
        else:
            chosen[board].append(item)
        protected.add(id(item))
        return True

    # Floor-weight sources: at least one slot each.
    for board in chosen:
        for item in list(spare[board]):
            publisher = item["publisher"]
            if info[publisher]["floor"] and not any(c["publisher"] == publisher for c in chosen[board]):
                swap_in(board, item)
    # Cold sources: at least ceil(20 %) of the slots, as far as there are cold candidates.
    pool = sum(1 for b in chosen for item in chosen[b] + spare[b] if info[item["publisher"]]["cold"])
    target = min(math.ceil(EXPLORE_SHARE * limit), pool)
    have = sum(1 for b in chosen for item in chosen[b] if info[item["publisher"]]["cold"])
    if have >= target:
        return
    # Cold sources take turns (newest first each), across boards, in board order.
    queues = {}
    for board in spare:
        for item in spare[board]:
            if info[item["publisher"]]["cold"]:
                queues.setdefault(item["publisher"], []).append((board, item))
    for board, item in smooth_round_robin(queues, dict.fromkeys(queues, 1.0), pool):
        if have >= target:
            break
        if swap_in(board, item):
            have += 1
