"""Phase 6 experiment grid. Every variant starts from the exact production
per-index config (fno_signals.config.strategy_config_for) and changes one
family of parameters at a time, so each result is directly comparable to
the baseline. `neighbors` records each one-dimensional family's ordered
parameter axis for the robustness check."""

from __future__ import annotations

from dataclasses import replace

from fno_signals.config import StrategyConfig
from research.phase6.engine import AdxFilter, Variant, paper_session_variant, with_risk, with_signal


def build_variants(base: StrategyConfig) -> tuple[list[Variant], dict[str, list[str]]]:
    variants: list[Variant] = [Variant("baseline", base, family="baseline")]
    families: dict[str, list[str]] = {}

    def add(family: str, name: str, config: StrategyConfig, **kw) -> None:
        variants.append(Variant(name, config, family=family, **kw))
        families.setdefault(family, []).append(name)

    # EMA fast/slow (baseline 9/21)
    for fast, slow in [(5, 13), (8, 21), (9, 21), (12, 26), (9, 30), (20, 50)]:
        name = "baseline" if (fast, slow) == (9, 21) else f"ema {fast}/{slow}"
        if name == "baseline":
            families.setdefault("ema", []).append(name)
            continue
        add("ema", name, with_signal(base, ema_fast_length=fast, ema_slow_length=slow))

    # RSI length (baseline 14)
    for length in [7, 10, 14, 21]:
        if length == 14:
            families.setdefault("rsi_length", []).append("baseline")
            continue
        add("rsi_length", f"rsi len {length}", with_signal(base, rsi_length=length))

    # RSI thresholds (baseline 55/45)
    for bull, bear in [(50, 50), (52, 48), (55, 45), (58, 42), (60, 40)]:
        if (bull, bear) == (55, 45):
            families.setdefault("rsi_threshold", []).append("baseline")
            continue
        add("rsi_threshold", f"rsi {bull}/{bear}", with_signal(base, rsi_bull=bull, rsi_bear=bear))

    # Supertrend multiplier (baseline 3.0) and length (baseline 10)
    for mult in [2.0, 2.5, 3.0, 3.5, 4.0]:
        if mult == 3.0:
            families.setdefault("st_mult", []).append("baseline")
            continue
        add("st_mult", f"st mult {mult}", with_signal(base, supertrend_multiplier=mult))
    for length in [7, 10, 14, 20]:
        if length == 10:
            families.setdefault("st_len", []).append("baseline")
            continue
        add("st_len", f"st len {length}", with_signal(base, supertrend_length=length))

    # Stop distance, keeping the 1:3 reward:risk (baseline sl 1.5 / tp 4.5)
    for sl in [1.0, 1.25, 1.5, 2.0, 2.5]:
        if sl == 1.5:
            families.setdefault("sl_atr", []).append("baseline")
            continue
        add("sl_atr", f"sl {sl}xATR (1:3)", with_risk(base, sl_multiplier=sl, tp_multiplier=sl * 3))

    # Reward:risk at the baseline stop (baseline 1:3)
    for rr in [1.0, 1.5, 2.0, 3.0, 4.0]:
        if rr == 3.0:
            families.setdefault("reward_risk", []).append("baseline")
            continue
        add("reward_risk", f"tp {rr:g}R", with_risk(base, tp_multiplier=1.5 * rr))

    # Risk ATR length (baseline 14)
    for length in [7, 10, 14, 21]:
        if length == 14:
            families.setdefault("atr_len", []).append("baseline")
            continue
        add("atr_len", f"atr len {length}", with_risk(base, atr_length=length))

    # VWAP (canonical option, OFF in production). On index spot data with
    # zero volume this blocks every signal - measured, not assumed.
    add("vwap", "vwap ON", with_signal(base, use_vwap=True))

    # ADX / DI+ / DI- - research-only filter, not in the canonical strategy.
    for threshold in [15, 20, 25, 30]:
        add("adx_threshold", f"adx>{threshold} +DI", base, adx=AdxFilter(threshold=threshold))
    for length in [10, 14, 20]:
        add("adx_length", f"adx({length})>20 +DI", base, adx=AdxFilter(length, length, threshold=20))
    add("adx_other", "adx>20 no-DI", base, adx=AdxFilter(threshold=20, require_di=False))
    add("adx_other", "DI only", base, adx=AdxFilter(threshold=0, require_di=True))
    add("adx_other", "adx>20 +DI setup-mode", base, adx=AdxFilter(threshold=20, mode="setup"))

    # A small number of justified combinations (no blind grid search).
    add("combo", "adx>20 +DI, tp 2R", with_risk(base, tp_multiplier=3.0), adx=AdxFilter(threshold=20))
    add("combo", "adx>20 +DI, st mult 2.5", with_signal(base, supertrend_multiplier=2.5),
        adx=AdxFilter(threshold=20))
    add("combo", "rsi 60/40, tp 2R", with_risk(with_signal(base, rsi_bull=60, rsi_bear=40), tp_multiplier=3.0))

    # Execution realism for the baseline: what paper Auto Trade actually does.
    baseline = variants[0]
    # "paper session" = current paper Auto Trade (15:00 cutoff, 15:20 square-off,
    # SL/TP-level fills). "pre-fix close fill" = paper's exit pricing before
    # Phase 6, kept only to show how much that difference mattered.
    for fill, suffix in [("level", "paper session"), ("bar_close", "paper session, pre-fix close fill")]:
        v = paper_session_variant(baseline, exit_fill=fill, suffix=suffix)
        variants.append(replace(v, family="realism"))
    variants.append(Variant("baseline gap-aware fill", base, exit_fill="gap_aware", family="realism"))
    return variants, families
