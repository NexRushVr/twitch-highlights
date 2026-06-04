"""Fun, Docker-style clip slugs: <adjective>-<noun> (e.g. "sneaky-otter").

Used to name clips `<streamer>-<adjective>-<noun>.mp4` instead of an opaque hex
token, so a highlight has a memorable, shareable handle. ~70x70 ≈ 4900 combos —
ample uniqueness for a run's handful of clips; on the rare collision we append a
short token.
"""

import secrets

ADJECTIVES = [
    "sneaky", "cosmic", "jolly", "feral", "spicy", "sleepy", "zesty", "rowdy",
    "groovy", "plucky", "snazzy", "rascally", "wobbly", "cheeky", "frosty",
    "turbo", "mellow", "dapper", "gnarly", "bouncy", "scrappy", "fuzzy", "swift",
    "goofy", "stealthy", "lucky", "rogue", "chunky", "breezy", "feisty", "moody",
    "noble", "perky", "sly", "vivid", "witty", "zany", "brave", "clever", "cozy",
    "daring", "epic", "glitchy", "hyper", "icy", "lofty", "nifty", "quirky",
    "rumbling", "sparkly", "thunderous", "untamed", "velvet", "wild", "yawning",
    "blazing", "crispy", "drowsy", "electric", "fancy", "giddy", "humble",
    "jazzy", "kooky", "loopy", "mighty", "nimble", "peppy", "salty", "tipsy",
]

NOUNS = [
    "otter", "walrus", "goblin", "raccoon", "narwhal", "yeti", "gremlin",
    "panther", "wombat", "dragon", "ferret", "kraken", "moose", "newt",
    "octopus", "penguin", "quokka", "badger", "lemur", "manatee", "gecko",
    "hedgehog", "ibex", "jackal", "koala", "llama", "meerkat", "okapi",
    "pangolin", "axolotl", "bison", "cougar", "dingo", "emu", "falcon",
    "gopher", "heron", "iguana", "jaguar", "kestrel", "lynx", "mongoose",
    "nautilus", "ocelot", "puffin", "quail", "rhino", "sloth", "tapir",
    "urchin", "viper", "weasel", "xerus", "yak", "zebu", "bandit", "comet",
    "goblin", "muffin", "noodle", "pickle", "rascal", "scamp", "trickster",
    "wizard", "phantom", "rocket", "biscuit", "gizmo", "waffle", "pretzel",
]


def random_slug() -> str:
    return f"{secrets.choice(ADJECTIVES)}-{secrets.choice(NOUNS)}"


def unique_slug(used: set) -> str:
    """A fresh slug not already in `used`; adds it to the set and returns it."""
    for _ in range(50):
        s = random_slug()
        if s not in used:
            used.add(s)
            return s
    s = f"{random_slug()}-{secrets.token_hex(2)}"   # vanishingly unlikely fallback
    used.add(s)
    return s
