"""Rebuild the notes search index from disk: docker exec studyvault python -m app.reindex"""
from . import db, notes_fs

if __name__ == "__main__":
    conn = db.connect()
    print(f"reindexed {notes_fs.reindex_all(conn)} note files")
    conn.close()
