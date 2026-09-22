def test_export_markdown_and_page(client):
    client.post("/courses/D413/competencies/import", data={"text": "Explains 802.11"})
    client.post("/courses/D413/cards", data={"front": "SSH port?", "back": "22"})
    client.post("/courses/D413/quizzes/questions/new", data={"kind": "mc", "prompt": "Band?", "choices": "* 5 GHz\n900 MHz"})
    r = client.get("/courses/D413/export.md")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert 'filename="D413-telecomm-and-wireless-communications-' in r.headers["content-disposition"]
    md = r.text
    for s in ["# D413 · Telecomm and Wireless Communications", "## Competency confidence", "| 1 | Explains 802.11 |",
              "## Overview", "## Mistakes", "**Q:** SSH port?", "**5 GHz ✓**"]:
        assert s in md, s
    assert "\n### Explains 802.11" in md  # note headings demoted one level
    page = client.get("/courses/D413/export")
    assert page.status_code == 200 and "Print / save as PDF" in page.text
