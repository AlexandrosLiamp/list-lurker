"""Per-listing post-scan pipeline: print + deal check + optional AI verification
+ Discord alert. Shared by every watch source (Skroutz/Insomnia/Vendora
categories in the main loop, Vinted, and the Facebook thread), so the alert
logic lives in one place.

Lives outside watch.py because the Facebook worker (crawlers/facebook.py) also
calls it — putting it in watch.py would make watch ↔ facebook circular."""

import ai_verify
from alerts import DISCORD_WEBHOOK, send_discord


def _process_new_listing(item, *, kind, deal_fn, bpage, ai_client,
                         notified, verified):
    """Print one new listing, run the deal check, optionally AI-verify it, and fire
    a Discord alert if it qualifies. Shared by every watch source (Skroutz/Insomnia/
    Vendora categories and the Facebook scan) so the alert pipeline lives in one
    place. `notified`/`verified` are the loop-level dedupe sets, mutated in place."""
    reason, ppr = deal_fn(item)
    price_str = f"{item['price']:.2f} €" if item["price"] else "?"
    deal_tag  = f"  *** DEAL: {reason} ***" if reason else ""
    cond      = f"[{item['condition'][:20]}]" if item["condition"] else ""
    print(f"    {price_str:>10} {cond:<22} {item['name'][:50]}{deal_tag}", flush=True)

    if not reason or item["url"] in notified:
        return

    # Default alert target = the listing as scraped
    targets = [(item, reason, ppr)]

    # ── Layer 2: AI verification (GPU deals only — only GPU produces a `reason`) ──
    # Open the real listing and let the model say what the item actually IS. This is
    # the bulletproof backstop for anything Layer 1 missed: a laptop / prebuilt PC /
    # mobile GPU / accessory that merely *mentions* a GPU model gets suppressed here.
    # If the model is unavailable or errors, we fall back to alerting (Layer 1 already
    # vetted the name) rather than going silent.
    if (kind == "gpu" and ai_client is not None and bpage is not None
            and item["url"] not in verified):
        verified.add(item["url"])
        try:
            verdict = ai_verify.verify_gpu_card(bpage, item["url"], item["name"])
        except Exception as e:
            verdict = None
            print(f"    ↳ [ai] verify error: {str(e)[:90]}", flush=True)

        if verdict is not None:
            if not verdict.available:
                print("    ↳ [ai] listing is sold/closed — alert suppressed", flush=True)
                notified.add(item["url"])
                return
            if not verdict.is_card:
                print(f"    ↳ [ai] NOT a standalone GPU card (category="
                      f"{verdict.category!r}) — alert suppressed", flush=True)
                notified.add(item["url"])
                return
            # AI confirms a real card. If it read a different asking price, re-check the
            # deal at that price (catches deposit/teaser prices that aren't real deals).
            if verdict.price and abs(verdict.price - (item["price"] or 0)) > 1:
                pseudo = {**item, "price": verdict.price}
                r2, p2 = deal_fn(pseudo)
                if not r2:
                    print(f"    ↳ [ai] corrected price {verdict.price:.0f}€ is no longer "
                          f"a deal — suppressed", flush=True)
                    notified.add(item["url"])
                    return
                targets = [(pseudo, r2, p2)]
                print(f"    ↳ [ai] verified GPU card; price corrected → {verdict.price:.0f}€",
                      flush=True)
            else:
                print(f"    ↳ [ai] verified standalone GPU card "
                      f"(category={verdict.category!r})", flush=True)

    for tgt, tgt_reason, tgt_ppr in targets:
        extra = []
        if tgt_ppr is not None:
            extra = [{"name": "PPR", "value": f"{tgt_ppr:.3f}", "inline": True}]
        send_discord(tgt, tgt_reason, extra_fields=extra)
        print(f"    ↳ Discord alert {'sent' if DISCORD_WEBHOOK else '(no webhook)'}", flush=True)
    notified.add(item["url"])
