"""Navigation: the current section is marked, secondary pages sit under More, course tabs keep every URL."""
import re

from app import web


def test_nav_section_by_path():
    for path, key in [("/", "today"), ("/terms", "courses"), ("/terms/1", "courses"), ("/courses/D413/cards", "courses"),
                      ("/quizzes/3", "courses"), ("/review", "review"), ("/ask", "ask"), ("/ask/12", "ask"),
                      ("/ask/memory", "more"), ("/search", "search"), ("/sessions", "more"), ("/certs", "more"),
                      ("/settings", "more"), ("/asking", ""), ("/nowhere", "")]:
        assert web.nav_section(path) == key, path


def test_current_page_is_marked(client):
    nav = lambda url: re.search(r'<nav class="nav" aria-label="Main">(.*?)</nav>', client.get(url).text, re.S).group(1)  # noqa: E731
    assert re.findall(r'<a href="([^"]+)" class="active" aria-current="page"', nav("/")) == ["/"]
    assert re.findall(r'<a href="([^"]+)" class="active" aria-current="page"', nav("/review")) == ["/review"]
    assert re.findall(r'<a href="([^"]+)" class="active" aria-current="page"', nav("/courses/D413/cards")) == ["/terms"]
    assert re.findall(r'<a href="([^"]+)" class="active" aria-current="page"', nav("/tasks")) == ["/tasks"]
    assert re.findall(r'<a href="([^"]+)" class="active" aria-current="page"', nav("/courses/D413/tasks")) == ["/terms"]
    assert '<summary class="active">More' in nav("/settings")
    assert 'aria-current="page">Settings' in nav("/settings")
    assert 'class="active"' not in nav("/certs").split("<details")[0]  # More's own pages don't light a main item


def test_secondary_pages_are_under_more(client):
    html = client.get("/").text
    menu = re.search(r'<div class="more-menu">(.*?)</div>', html, re.S).group(1)
    for href in ("/sessions", "/certs", "/settings"):
        assert f'href="{href}"' in menu
    assert 'action="/logout"' in menu
    assert 'class="skip-link" href="#main"' in html and 'id="main"' in html


def test_course_tabs_are_grouped_and_every_url_still_works(client):
    page = client.get("/courses/D413/cards").text
    assert 'aria-label="Practice pages"' in page and 'aria-label="Notes pages"' not in page
    assert re.search(r'<a href="/courses/D413/cards" class="active" aria-current="page">Cards', page)
    assert re.search(r'<a href="/courses/D413/cards" class="active" aria-current="true">Practice', page)
    notes = client.get("/courses/D413/notes/mistakes").text
    assert 'aria-label="Notes pages"' in notes
    assert re.search(r'<a href="/courses/D413/notes/mistakes" class="active" aria-current="page">Mistakes', notes)
    for url in ("/courses/D413", "/courses/D413/competencies", "/courses/D413/notes/notebook",
                "/courses/D413/notes/mistakes", "/courses/D413/cards", "/courses/D413/quizzes", "/courses/D413/assessment",
                "/courses/D413/notes/overview"):
        assert client.get(url).status_code == 200, url
