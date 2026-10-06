"""IR-site scraping.

bursamalaysia.com sits behind a Cloudflare Turnstile challenge on every path
checked (including ``robots.txt`` itself) - confirmed by direct browser
navigation. Automating past a CAPTCHA is not something this codebase does, so
company investor-relations websites are the only automated source; see
``ir_fallback.py`` and ``run_annual_reports.py``.
"""
