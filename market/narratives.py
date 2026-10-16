"""Narratives and sectors: which theme a coin belongs to (from name, symbol and description) and which words
are suddenly everywhere in new launches (emergent narratives, e.g. "tesla roadster" in 10/2026).

Pure functions, no network. Sector = fixed lexicon (stable, comparable over weeks); terms = data-driven
(new memes appear faster than any lexicon)."""
import math
import re
from collections import Counter, defaultdict

# sector -> (label, whole-word keywords, roots that also count inside glued names like "toiletjak")
SECTORS = {
    "ai": ("KI & Agenten", {"ai", "agent", "agents", "gpt", "chatgpt", "grok", "llm", "agi", "neural", "intelligence",
                            "openai", "claude", "deepseek", "sentient", "robot", "robo", "android", "cyborg", "model",
                            "superintelligence", "algo", "quantum", "brain", "mind", "aixbt", "virtual"},
           {"grok", "gpt", "agent"}),
    "dog": ("Hunde", {"dog", "dogs", "doge", "inu", "shiba", "puppy", "pup", "wif", "bonk", "corgi", "pug", "husky",
                      "doggo", "pomeranian", "retriever", "poodle", "chihuahua", "woof", "bark", "dogwif"},
            {"doge", "shiba", "bonk", "doggo", "puppy"}),
    "cat": ("Katzen", {"cat", "cats", "kitty", "kitten", "meow", "popcat", "mew", "nyan", "catwif", "purr", "tom"},
            {"kitty", "meow", "popcat"}),
    "frog": ("Frösche & Pepe", {"pepe", "frog", "kek", "toad", "ribbit", "apu", "pepito"}, {"pepe", "frog"}),
    "animal": ("Andere Tiere", {"monkey", "ape", "chimp", "penguin", "pengu", "hippo", "deng", "bear", "fish", "duck",
                                "goat", "horse", "panda", "rabbit", "bunny", "hamster", "pnut", "squirrel", "otter",
                                "capybara", "seal", "whale", "shark", "bird", "owl", "pig", "cow", "sheep", "lion",
                                "tiger", "wolf", "fox", "turtle", "snail", "crab", "rat", "mouse", "beaver", "koala"},
               {"pengu", "hippo", "capybara", "monkey"}),
    "politics": ("Politik", {"trump", "maga", "biden", "kamala", "usa", "america", "american", "president", "vote",
                             "election", "congress", "potus", "melania", "barron", "vance", "senate", "government",
                             "states", "united", "nation", "republic", "patriot", "liberty", "freedom", "putin",
                             "china", "israel", "iran", "tariff", "dome"},
                 {"trump", "maga", "melania"}),
    "celeb": ("Promis & Elon", {"elon", "musk", "tesla", "spacex", "starman", "roadster", "kanye", "drake", "mrbeast",
                                "taylor", "swift", "snoop", "diddy", "tate", "zuck", "bezos", "saylor", "cz", "vitalik",
                                "ansem", "kardashian", "bieber", "logan", "paul", "ronaldo", "messi", "neymar"},
              {"elon", "musk", "tesla"}),
    "anime": ("Anime & Japan", {"anime", "chan", "san", "kun", "sama", "waifu", "senpai", "kawaii", "japan", "ninja",
                                "samurai", "manga", "otaku", "sakura", "tokyo", "neko", "baka", "chibi"},
              {"waifu", "senpai", "kawaii"}),
    "gaming": ("Gaming", {"game", "games", "gaming", "gamer", "pixel", "arcade", "minecraft", "roblox", "fortnite",
                          "nintendo", "mario", "sonic", "pokemon", "zelda", "quest", "boss", "level", "runner",
                          "league", "player", "steam"},
               {"pixel", "arcade"}),
    "culture": ("Internet-Kultur", {"meme", "memes", "wojak", "jak", "chad", "gigachad", "based", "cope", "seethe", "wtf",
                                    "lol", "lmao", "rizz", "sigma", "aura", "normie", "npc", "skibidi", "brainrot", "cult",
                                    "tiktok", "viral", "gm", "gn", "asf", "bro", "fren", "frens", "vibe", "vibes",
                                    "goon", "edge", "ngmi", "wagmi", "hawk", "tuah", "sus", "simp", "boomer", "zoomer",
                                    "doomer", "rage", "troll", "pump", "dump"},
                {"wojak", "jak", "chad", "rizz", "skibidi", "brainrot"}),
    "space": ("Weltraum", {"space", "mars", "rocket", "nasa", "alien", "aliens", "ufo", "star", "stars", "galaxy",
                           "cosmic", "cosmos", "planet", "orbit", "astronaut", "lunar", "saturn", "jupiter", "flying"},
              {"rocket", "galaxy", "alien"}),
    "food": ("Essen & Alltag", {"pizza", "burger", "taco", "banana", "apple", "coffee", "beer", "cheese", "sushi", "bread",
                                "cake", "cookie", "donut", "toilet", "fan", "car", "pens", "overalls", "chair", "shoe",
                                "hat", "milk", "egg", "rice", "noodle", "soup", "wine", "tea"},
               {"pizza", "burger", "toilet"}),
    "finance": ("Finanzen & Reserve", {"fund", "reserve", "bank", "dollar", "gold", "capital", "treasury", "cash",
                                       "money", "stock", "etf", "rich", "wealth", "usd", "usdc", "usdt", "yield", "vault",
                                       "silver", "oil", "water"},
                 {"reserve", "treasury"}),
    "crypto": ("Krypto-Meta", {"sol", "solana", "btc", "bitcoin", "eth", "ethereum", "dex", "chain", "crypto", "degen",
                               "airdrop", "fees", "dev", "devs", "holders", "jeet", "jeets", "pumpfun", "bonding",
                               "memecoin", "ticker", "rug", "moon", "lambo", "hodl", "satoshi", "nakamoto", "block"},
               {"solana", "bitcoin", "memecoin", "pumpfun"}),
}
OTHER = "other"
SECTOR_LABEL = {k: v[0] for k, v in SECTORS.items()} | {OTHER: "Sonstige"}

STOP = set("""
the this that these those with for and but not are was were will would have has had just get got all any one two
out now new our his her him she they them their from what who why how can could too very more most made make
makes name named ticker its you your yours yes ever never also into onto over under about after before again
then than there here when where while been being some such only own same off per via let lets let's like
real official first last day days time times year years big small little best good great top super mega ultra
token tokens coin coins official the of to in on at by is it as an or be do go no so up my me we us
der die das und ist ein eine nicht mit
""".split())
TERM_STOP = STOP | {"coin", "token", "sol", "solana", "pump", "fun", "pumpfun", "test", "hello", "world", "saying"}

_WORD = re.compile(r"[a-z][a-z0-9]*")


def words(text):
    """Lowercase words of 2+ letters; CamelCase is split ('FlyingTesla' -> flying, tesla)."""
    if not text:
        return []
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(text))
    return [w for w in _WORD.findall(text.lower()) if len(w) >= 2 and not w.isdigit()]


def classify(name="", symbol="", description=""):
    """-> (primary sector, {sector: hits}). Name and symbol count double, the description once; roots also
    match inside glued words of 5+ letters. Ties go to the sector listed first in SECTORS."""
    hits = Counter()
    head = words(name) + words(symbol)
    for weight, ws in ((2, head), (1, words(description)[:60])):
        for w in ws:
            for key, (_, kw, roots) in SECTORS.items():
                if w in kw:
                    hits[key] += weight
                elif len(w) >= 5 and any(r in w for r in roots):
                    hits[key] += weight
    if not hits:
        return OTHER, {}
    order = list(SECTORS)
    best = max(hits, key=lambda k: (hits[k], -order.index(k)))
    return best, dict(hits)


def terms_of(name="", symbol=""):
    """Narrative terms of one coin: name words without stop words, plus adjacent name bigrams ('tesla roadster')."""
    ws = [w for w in words(name) if len(w) >= 3 and w not in TERM_STOP]
    out = set(ws)
    out.update(f"{a} {b}" for a, b in zip(ws, ws[1:]))
    s = (symbol or "").lower()
    if len(s) >= 3 and s.isalpha() and s not in TERM_STOP:
        out.add(s)
    return out


def emergent_terms(recent, base, outcomes=(), volume=None, min_count=8, keep=40, prior_k=10):
    """Words that are suddenly frequent.

    recent / base: lists of (mint, name, symbol) of launches in the recent and in the (longer) base window
    outcomes: (name, symbol, hit2x 0/1, rugged 0/1) of launches whose outcome is already known (no lookahead)
    volume: {term: h1 USD volume of traded coins carrying it}
    -> rows sorted by heat: {term, n_recent, n_base, lift, hit2x, rug, n_outcomes, volume_h1_usd, heat, mints}
    lift = share of recent launches with the term / share in the base window (both +1 smoothed);
    hit2x is shrunk towards the overall rate with `prior_k` pseudo launches, so 3 lucky coins do not make a narrative.
    """
    rc, rm = Counter(), defaultdict(list)
    for mint, name, sym in recent:
        for t in terms_of(name, sym):
            rc[t] += 1
            if len(rm[t]) < 8:
                rm[t].append(mint)
    bc = Counter()
    for _, name, sym in base:
        bc.update(terms_of(name, sym))
    nr, nb = max(1, len(recent)), max(1, len(base))
    oc, oh, orr = Counter(), Counter(), Counter()
    tot_n = tot_h = tot_r = 0
    for name, sym, hit, rug in outcomes:
        tot_n += 1
        tot_h += hit
        tot_r += rug
        for t in terms_of(name, sym):
            oc[t] += 1
            oh[t] += hit
            orr[t] += rug
    base_hit = tot_h / tot_n if tot_n else None
    base_rug = tot_r / tot_n if tot_n else None
    volume = volume or {}
    rows = []
    for t, n in rc.items():
        if n < min_count:
            continue
        # a bigram that always comes with its words adds nothing: keep it only if it is most of their uses
        if " " in t and any(n < 0.5 * rc.get(w, 0) for w in t.split()):
            continue
        lift = ((n + 1) / nr) / ((bc.get(t, 0) + 1) / nb)
        hit = rug = None
        if base_hit is not None:
            hit = (oh[t] + base_hit * prior_k) / (oc[t] + prior_k)
            rug = (orr[t] + base_rug * prior_k) / (oc[t] + prior_k)
        vol = float(volume.get(t) or 0.0)
        heat = term_heat(n, nr, lift, hit, base_hit, vol)
        rows.append({"term": t, "n_recent": n, "n_base": bc.get(t, 0), "lift": round(lift, 3),
                     "hit2x": None if hit is None else round(hit, 4), "rug": None if rug is None else round(rug, 4),
                     "n_outcomes": oc[t], "volume_h1_usd": round(vol, 2), "heat": heat, "mints": rm[t]})
    # drop single words that only live inside a stronger bigram ('roadster' next to 'tesla roadster')
    big = {w: r for r in rows if " " in r["term"] for w in r["term"].split()}
    rows = [r for r in rows if not (r["term"] in big and r["n_recent"] <= big[r["term"]]["n_recent"] * 1.2)]
    rows.sort(key=lambda r: -r["heat"])
    return rows[:keep]


def term_heat(n, n_total, lift, hit, base_hit, volume_usd):
    """0..100: how hot a narrative is right now. Mostly the lift vs the base window (a word that was always
    common is not news), then spread, money actually trading it, and whether its launches beat the average."""
    share = min(1.0, (n / max(1, n_total)) / 0.05)                 # 5 % of all launches = full
    lift_s = max(0.0, min(1.0, math.log(max(lift, 1e-9), 2) / 2))  # 4x = full, at or below 1x = 0
    vol_s = max(0.0, min(1.0, math.log10(1 + volume_usd) / 6))     # $1M h1 = full
    edge = 0.5
    if hit is not None and base_hit:
        edge = max(0.0, min(1.0, 0.5 + (hit / base_hit - 1)))      # 1.5x the base hit rate = full
    return round(100 * (0.50 * lift_s + 0.20 * share + 0.15 * vol_s + 0.15 * edge), 1)
