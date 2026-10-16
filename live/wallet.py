"""Trading-wallet key handling. The key is read only on the Mac (keypair file, .env or macOS Keychain), held in
memory by the executor and never logged, printed, stored in the database or sent anywhere but into a local
signature. `new_wallet` creates a separate trading wallet so the main holdings never sit in a hot key."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

DEFAULT_KEYFILE = Path.home() / ".config" / "mayhambot" / "trading-keypair.json"


class KeyError_(RuntimeError):
    pass


def _from_text(text: str):
    from solders.keypair import Keypair  # type: ignore

    t = text.strip()
    if t.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(t)))
    return Keypair.from_base58_string(t)


def load_keypair(keypair_path: str = "", private_key: str = "", keychain_service: str = ""):
    """Keypair from (first match) a keypair JSON file, the macOS Keychain, or SOLANA_PRIVATE_KEY. None if none set.
    Errors never include key material."""
    try:
        if keypair_path:
            p = Path(os.path.expanduser(keypair_path))
            mode = p.stat().st_mode & 0o077
            if mode:
                os.chmod(p, 0o600)  # readable by others: tighten instead of refusing
            return _from_text(p.read_text())
        if keychain_service:
            r = subprocess.run(["security", "find-generic-password", "-s", keychain_service, "-w"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                raise KeyError_(f"Keychain-Eintrag '{keychain_service}' nicht gefunden")
            return _from_text(r.stdout)
        if private_key:
            return _from_text(private_key)
    except KeyError_:
        raise
    except Exception as e:  # noqa: BLE001 - never echo the key
        raise KeyError_(f"Schlüssel nicht lesbar ({type(e).__name__})") from None
    return None


def pubkey_of(keypair) -> str:
    return str(keypair.pubkey())


def new_wallet(path: Path = DEFAULT_KEYFILE, overwrite: bool = False) -> str:
    """Creates a fresh trading keypair file (chmod 600, outside the repo) and returns only its public key."""
    from solders.keypair import Keypair  # type: ignore

    path = Path(os.path.expanduser(str(path)))
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} existiert schon (Pubkey: {pubkey_of(_from_text(path.read_text()))})")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    kp = Keypair()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(list(bytes(kp))))
    return pubkey_of(kp)


def set_env(env_file: Path, values: dict[str, str]) -> None:
    """Sets keys in .env (replaces the first occurrence, drops duplicates, appends missing ones), chmod 600.
    Never prints values."""
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    out, done = [], set()
    for line in lines:
        k = line.split("=", 1)[0].strip()
        if k in values:
            if k not in done:
                out.append(f"{k}={values[k]}")
                done.add(k)
            continue
        out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in done]
    env_file.write_text("\n".join(out) + "\n")
    os.chmod(env_file, 0o600)
