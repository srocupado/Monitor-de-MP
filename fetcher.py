import re
import logging
from datetime import date

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

MONTHS_PT = {
    1: "JANEIRO", 2: "FEVEREIRO", 3: "MARÇO", 4: "ABRIL",
    5: "MAIO", 6: "JUNHO", 7: "JULHO", 8: "AGOSTO",
    9: "SETEMBRO", 10: "OUTUBRO", 11: "NOVEMBRO", 12: "DEZEMBRO",
}

PLANALTO_BASE = "https://www.planalto.gov.br"
DOU_BASE = "https://www.in.gov.br"

# Full browser-like headers to reduce chance of 403
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "DNT": "1",
}


def _make_session(referer: str | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    if referer:
        s.headers["Referer"] = referer
    return s


def _planalto_period(year: int) -> str:
    start = ((year - 1991) // 4) * 4 + 1991
    return f"{start}-{start + 3}"


def _format_date_pt(d: date) -> str:
    day_str = f"{d.day}º" if d.day == 1 else str(d.day)
    return f"{day_str} DE {MONTHS_PT[d.month]} DE {d.year}"


def _format_date_dou(d: date) -> str:
    """Format date as DD/MM/YYYY for DOU query parameters."""
    return d.strftime("%d/%m/%Y")


def _extract_numero(text: str, href: str) -> str:
    m = re.search(r"N[ºo°]?\s*([\d\.]+)", text, re.IGNORECASE)
    if m:
        return m.group(1).replace(".", "")
    m = re.search(r"mpv(\d+)-", href.lower())
    if m:
        return m.group(1)
    return "???"


def _fetch_mp_page(url: str, session: requests.Session | None = None) -> tuple[str, str]:
    sess = session or _make_session()
    try:
        resp = sess.get(url, timeout=20)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        ementa_lines = []
        for ln in lines[:20]:
            if ln.upper().startswith("MEDIDA PROVISÓRIA") or ln.upper().startswith("A PRESIDENTA") or ln.upper().startswith("O PRESIDENTE"):
                break
            if len(ln) > 30:
                ementa_lines.append(ln)
        ementa = " ".join(ementa_lines[:3]) if ementa_lines else lines[0] if lines else ""
        return ementa, "\n".join(lines[:500])
    except Exception as exc:
        logger.warning("Erro ao buscar página da MP (%s): %s", url, exc)
        return "", ""


# ── Source 1: Planalto ────────────────────────────────────────────────────────

def _fetch_planalto(target_date: date) -> list[dict] | None:
    """Scrapes the Planalto MP index.

    Returns list of MPs, empty list if none found today, or None if unreachable.
    """
    year = target_date.year
    period = _planalto_period(year)
    index_url = f"{PLANALTO_BASE}/ccivil_03/_Ato{period}/{year}/Mpv/"
    date_str = _format_date_pt(target_date)

    logger.info("Consultando Planalto: %s", index_url)
    session = _make_session()
    resp = None
    # Try mixed-case then lowercase (Planalto sometimes redirects to lowercase)
    for url_attempt in [index_url, index_url.lower()]:
        try:
            session.get(PLANALTO_BASE, timeout=15)
            r = session.get(url_attempt, timeout=20, allow_redirects=True)
            if r.status_code == 200:
                resp = r
                break
        except requests.RequestException:
            pass

    if resp is None:
        logger.warning("Planalto indisponível – tentando DOU.")
        return None

    resp.encoding = resp.apparent_encoding or "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for link in soup.find_all("a", href=True):
        text = link.get_text(" ", strip=True)
        text_upper = text.upper()
        if "MEDIDA PROVIS" not in text_upper:
            continue
        if date_str not in text_upper:
            continue

        href = link["href"]
        if href.startswith("http"):
            mp_url = href
        else:
            href_clean = href.lstrip("./")
            mp_url = f"{PLANALTO_BASE}/ccivil_03/_Ato{period}/{year}/Mpv/{href_clean}"

        numero = _extract_numero(text_upper, href)
        logger.info("  [Planalto] MP nº %s: %s", numero, mp_url)
        ementa, texto = _fetch_mp_page(mp_url, session)

        results.append({
            "numero": numero,
            "ano": year,
            "ementa": ementa or text,
            "data_publicacao": target_date.isoformat(),
            "url_planalto": mp_url,
            "texto_integral": texto,
        })

    return results


# ── Source 2: DOU (Diário Oficial da União) ───────────────────────────────────

def _fetch_dou(target_date: date) -> list[dict]:
    """Scrapes the DOU search for MPs published on target_date.

    The DOU is the primary legal source — MPs are published there first,
    on the same day, before Planalto indexes them.
    """
    date_str_dou = _format_date_dou(target_date)
    year = target_date.year
    period = _planalto_period(year)

    # DOU search URL: searches for "Medida Provisória" in all sections for the date
    search_url = (
        f"{DOU_BASE}/consulta/-/busca/dou"
        f"?q=%22Medida+Provis%C3%B3ria%22"
        f"&s=do1%2Cdoe"          # Section 1 + Extra editions (where MPs are published)
        f"&publicationDate={date_str_dou}"
    )

    logger.info("Consultando DOU: %s", search_url)
    session = _make_session(referer=DOU_BASE)
    try:
        session.get(DOU_BASE, timeout=15)
        resp = session.get(search_url, timeout=25, allow_redirects=True)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
    except requests.RequestException as exc:
        logger.warning("DOU indisponível: %s", exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    seen = set()

    # DOU search results: each result is a card/article with title and metadata
    for item in soup.find_all(["article", "li", "div"], class_=re.compile(r"resultado|result|item|card", re.I)):
        text = item.get_text(" ", strip=True)
        text_upper = text.upper()
        if "MEDIDA PROVIS" not in text_upper:
            continue

        # Extract MP number
        m = re.search(r"MEDIDA PROVIS[ÓO]RIA\s+N[ºo°]?\s*([\d\.]+)", text_upper)
        if not m:
            continue
        numero = m.group(1).replace(".", "")
        if numero in seen:
            continue
        seen.add(numero)

        # Try to get the DOU article link
        link_tag = item.find("a", href=True)
        dou_url = ""
        if link_tag:
            href = link_tag["href"]
            dou_url = href if href.startswith("http") else f"{DOU_BASE}{href}"

        # Build expected Planalto URL from the MP number
        ano2d = str(year)[-2:]
        planalto_url = (
            f"{PLANALTO_BASE}/ccivil_03/_Ato{period}/{year}/Mpv/"
            f"mpv{numero}-{ano2d}.htm"
        )

        # Extract ementa from the result text
        ementa = text[:300].strip()

        # Try to get full text from Planalto (may fail if blocked)
        _, texto = _fetch_mp_page(planalto_url, session)
        if not texto and dou_url:
            _, texto = _fetch_mp_page(dou_url, session)

        logger.info("  [DOU] MP nº %s encontrada.", numero)
        results.append({
            "numero": numero,
            "ano": year,
            "ementa": ementa,
            "data_publicacao": target_date.isoformat(),
            "url_planalto": planalto_url,
            "texto_integral": texto or ementa,
        })

    if not results:
        logger.info("DOU acessível mas sem MPs em %s.", target_date.isoformat())
    return results


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_mps(target_date: date) -> list[dict]:
    """Fetch MPs published on target_date. Planalto first, DOU as fallback."""
    result = _fetch_planalto(target_date)
    if result is None:
        logger.info("Usando DOU como fonte alternativa.")
        return _fetch_dou(target_date)
    return result
