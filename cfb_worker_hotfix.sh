#!/bin/sh
set -eu

python - <<'PY'
from pathlib import Path

path = Path('/app/app/cfb_capper_preview.py')
text = path.read_text(encoding='utf-8')
old = '''        manual_fallback_picks = [
            row for row in (_normalize_legacy_bridge_pick(p) for p in manual_fallback_picks)
            if row is not None
        ]
        manual_semantics = {_semantic_fingerprint(p) for p in manual_fallback_picks}
        bridge_picks = [
            p for p in bridge_picks
            if _semantic_fingerprint(p) not in manual_semantics
        ]
        picks = [*bridge_picks, *[dict(p) for p in manual_fallback_picks]]
'''
new = '''        normalized_manual_fallback_picks = [
            row for row in (_normalize_legacy_bridge_pick(p) for p in manual_fallback_picks)
            if row is not None
        ]
        manual_semantics = {_semantic_fingerprint(p) for p in normalized_manual_fallback_picks}
        bridge_picks = [
            p for p in bridge_picks
            if _semantic_fingerprint(p) not in manual_semantics
        ]
        picks = [*bridge_picks, *[dict(p) for p in normalized_manual_fallback_picks]]
'''
if old in text:
    text = text.replace(old, new, 1)
    text = text.replace(
        '"manual_fallback_count": len(manual_fallback_picks),',
        '"manual_fallback_count": len(normalized_manual_fallback_picks),',
        1,
    )
    path.write_text(text, encoding='utf-8')
    print('CFB_CAPPER_SCOPE_HOTFIX_APPLIED', flush=True)
elif 'normalized_manual_fallback_picks = [' in text:
    print('CFB_CAPPER_SCOPE_HOTFIX_ALREADY_PRESENT', flush=True)
else:
    raise SystemExit('CFB_CAPPER_SCOPE_HOTFIX_FAILED: expected source block not found')
PY

exec /app/start.sh
