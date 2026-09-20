# ===========================================================================
# CLI
# ===========================================================================
def _build_argparser():
    import argparse
    p = argparse.ArgumentParser(description="ScrapingStack - orchestrateur unifié.")
    p.add_argument("urls", nargs="*", help="URLs à traiter")
    p.add_argument("--urls-file", help="Fichier texte (une URL par ligne)")
    p.add_argument("--proxies-file", help="Fichier de proxies")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--mode", choices=["thread", "process", "async"], default="thread")
    p.add_argument("--captcha-key", default=None,
                   help="Clé API CapSolver / 2Captcha")
    p.add_argument("--captcha-service", default="capsolver",
                   choices=["capsolver", "2captcha", "anticaptcha"])
    p.add_argument("--no-browser", action="store_true",
                   help="Désactiver le fallback navigateur")
    p.add_argument("--no-tls", action="store_true",
                   help="Désactiver curl_cffi")
    p.add_argument("--min-delay", type=float, default=0.4)
    p.add_argument("--max-delay", type=float, default=2.5)
    p.add_argument("--verbose", action="store_true")
    return p

