from __future__ import annotations

from pathlib import Path
import os

from execution.capital_reply_session import (
    find_default_env,
    find_skcom_dll,
    load_env_file,
)


def main() -> int:
    root = Path(__file__).resolve().parent
    env = find_default_env(root)
    if env:
        load_env_file(env)

    dll = find_skcom_dll(root)

    import comtypes.client
    comtypes.client.GetModule(str(dll))
    import comtypes.gen.SKCOMLib as sk

    iface = sk.ISKOrderLib
    keywords = ("Cancel", "Change", "Correct", "Delete", "OddLot", "StockOrder")

    print(f"DLL={dll}")
    print("Matching ISKOrderLib methods:")
    seen = set()

    for item in getattr(iface, "_methods_", []):
        name = getattr(item, "name", None) or getattr(item, "__name__", None)
        text = str(item)
        if name is None:
            # best-effort method-name extraction from comtypes COMMETHOD repr
            for token in text.replace("(", " ").replace(")", " ").replace(",", " ").split():
                if any(k.lower() in token.lower() for k in keywords):
                    name = token
                    break
        if name and any(k.lower() in name.lower() for k in keywords):
            if name not in seen:
                seen.add(name)
                print(name)
                print("  ", text)

    # Also list names exposed by dir(), useful when _methods_ repr differs by comtypes version.
    print()
    print("dir(ISKOrderLib) matches:")
    for name in sorted(dir(iface)):
        if any(k.lower() in name.lower() for k in keywords):
            print(name)

    print()
    print("NO ORDER WAS SENT OR CANCELLED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
