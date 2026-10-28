"""Live retrieval with exact quotes, provenance and explicit evidence gaps."""
import os
import re
import urllib.parse
from html.parser import HTMLParser

from .common import json_request, now, request


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.title, self.hidden, self.in_title = [], [], 0, False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.hidden += 1
        if tag == "title":
            self.in_title = True
        if tag in ("p", "h1", "h2", "h3", "li", "div", "br"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, text):
        if not self.hidden:
            self.parts.append(text)
            if self.in_title:
                self.title.append(text)


def retrieve(url, query):
    body, headers = request(url, timeout=12, public=True)
    content_type = headers.get("Content-Type", "")
    if not any(t in content_type for t in ("text/", "json", "xml")):
        raise ValueError("此版本仅核验 HTML/纯文本来源；PDF 需先提取原文。")
    p = Page()
    p.feed(body)
    paragraphs = [re.sub(r"\s+", " ", t).strip() for t in "".join(p.parts).splitlines() if t.strip()]
    terms = set(re.findall(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", query.lower()))
    # Chinese bigrams allow matching a natural-language question against paragraphs.
    for t in list(terms):
        if re.search(r"[\u4e00-\u9fff]", t):
            terms.update(t[i:i+2] for i in range(len(t)-1))
    ranked = sorted(enumerate(paragraphs, 1), key=lambda v: sum(t in v[1].lower() for t in terms), reverse=True)
    quotes = [{"location": f"正文抽取段落 {i}", "quote": text,
               "supports": "原文仅支持该引文直接表达的陈述；相关性不代表对问题的完整证明。"}
              for i, text in ranked[:4] if sum(t in text.lower() for t in terms) > 0]
    return {"title": "".join(p.title) or urllib.parse.urlsplit(url).hostname,
            "publisher": urllib.parse.urlsplit(url).hostname, "url": url,
            "published_or_version": headers.get("Last-Modified") or "原文未核实发布日期",
            "accessed_at": now(), "source_type": "网页原文（发布主体与一手/二手属性需结合内容判断）",
            "verified": True, "quotes": quotes, "scope": "网页抓取与逐字引用核验；不证明来源中所有主张为真"}


def run(payload):
    query = str(payload.get("query") or payload.get("text") or "").strip()
    if not query:
        return {"status": "awaiting-input", "missing": ["query：主题或问题"]}
    urls = payload.get("urls", [])
    if not isinstance(urls, list) or any(not isinstance(u, str) for u in urls):
        raise ValueError("urls 必须是 URL 字符串列表。")
    gaps, candidates = [], []
    if not urls:
        # Crossref offers real bibliographic discovery without inventing references.
        try:
            u = "https://api.crossref.org/works?rows=5&query=" + urllib.parse.quote(query)
            for item in json_request(u, timeout=12, public=True)["message"]["items"]:
                candidates.append({"title": " ".join(item.get("title", [])), "publisher": item.get("publisher"),
                                   "url": item.get("URL"), "doi": item.get("DOI"),
                                   "published_or_version": item.get("published", {}).get("date-parts"),
                                   "accessed_at": now(), "verified": False,
                                   "scope": "Crossref 题录候选；未读取全文，不作为结论证据。"})
        except Exception:
            gaps.append("Crossref 题录检索不可用；可提供公开 HTTPS 来源 URL 进行原文核验。")
        if any(t in query.lower() for t in ("acps", "梧桐", "智能体互联")):
            urls = ["https://www.aicomp.cn/tracks/tracks-5/5169.html"]
    sources = []
    for url in urls[:5]:
        try:
            sources.append(retrieve(url, query))
        except Exception:
            gaps.append(f"无法核验来源：{url}；访问失败、响应类型不支持或地址被拒绝。")
    if not any(s["quotes"] for s in sources):
        gaps.append("没有获得能直接支撑结论的全文引文；不生成已证实结论。")
    return {"status": "ok" if any(s["quotes"] for s in sources) else "evidence-insufficient",
            "query": query, "sources": sources, "bibliographic_candidates": candidates, "evidence_gaps": gaps}
