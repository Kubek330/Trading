"""`python -m memedesk doctor`: check every data source from this device."""
from __future__ import annotations

import time

from .config import Config
from .http import HttpClient

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"  # long-lived token, safe test subject


def checks(cfg: Config):
    rpc = f"https://mainnet.helius-rpc.com/?api-key={cfg.helius_api_key}" if cfg.helius_api_key else cfg.solana_rpc_url
    jup = ("https://api.jup.ag/tokens/v2", {"x-api-key": cfg.jup_api_key}) if cfg.jup_api_key \
        else ("https://lite-api.jup.ag/tokens/v2", None)
    out = [
        ("DexScreener", "required: pairs, prices", "GET", f"{cfg.dexscreener_url}/token-profiles/latest/v1", None, None,
         lambda d: f"{len(d)} fresh profiles"),
        ("GeckoTerminal", "required: discovery, trades, candles", "GET",
         f"{cfg.geckoterminal_url}/networks/solana/trending_pools", None, {"Accept": "application/json;version=20230302"},
         lambda d: f"{len(d.get('data', []))} trending pools"),
        ("RugCheck", "required: LP, dev, holders (filter kills everything without it)", "GET",
         f"{cfg.rugcheck_url}/tokens/{BONK}/report", None, None, lambda d: f"{len(d.get('topHolders') or [])} holders read"),
        ("Solana RPC" + (" (Helius)" if cfg.helius_api_key else " (public)"), "required: mint/freeze authority", "POST",
         rpc, {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo", "params": [BONK, {"encoding": "jsonParsed"}]},
         None, lambda d: "mint account read" if (d.get("result") or {}).get("value") else f"odd reply {str(d)[:60]}"),
        ("trench.bot", "optional: bundle %, falls back to RugCheck insiders", "GET",
         f"{cfg.trench_url}/bundle/bundle_advanced/{BONK}", None, None, lambda d: "bundle data ok"),
        ("Jupiter tokens" + (" (key)" if cfg.jup_api_key else " (no key)"), "optional: organic score", "GET",
         f"{jup[0]}/search?query={BONK}", None, jup[1],
         lambda d: f"organicScore {((d or [{}])[0] if isinstance(d, list) else {}).get('organicScore')}"),
    ]
    if cfg.coingecko_api_key:
        out.append(("CoinGecko Demo", "optional: backup when GeckoTerminal says 429", "GET",
                    "https://api.coingecko.com/api/v3/onchain/networks/solana/trending_pools", None,
                    {"x-cg-demo-api-key": cfg.coingecko_api_key}, lambda d: "key works"))
    if cfg.helius_api_key:
        out.append(("Helius enhanced API", "optional: fast whale-scout reads", "GET",
                    f"https://api.helius.xyz/v0/addresses/{BONK}/transactions?api-key={cfg.helius_api_key}&limit=1",
                    None, None, lambda d: "parsed transactions ok"))
    return out


def run(cfg: Config) -> int:
    http = HttpClient(timeout=15, max_backoff_s=3)
    bad_required = 0
    print(f"memedesk doctor · profile={cfg.profile} · min score {cfg.min_score:g}\n")
    for name, role, method, url, body, headers, ok in checks(cfg):
        t0 = time.time()
        try:
            data = http.request(method, url, json=body, headers=headers, retries=2)
            print(f"  ✓ {name:24} {time.time() - t0:5.1f}s  {ok(data)}")
        except Exception as e:  # report every failure, keep going
            msg = str(e).replace(cfg.helius_api_key or "\0", "***")
            print(f"  ✗ {name:24} {time.time() - t0:5.1f}s  {msg[:90]}\n      → {role}")
            bad_required += role.startswith("required")
    print("\n" + ("All required sources work. If there are still no trades, run: python -m memedesk kills -n 40"
                  if not bad_required else
                  f"{bad_required} required source(s) failing: the filter will kill coins until they're back "
                  "(mobile data/VPN/DNS often cause this)."))
    return 1 if bad_required else 0
