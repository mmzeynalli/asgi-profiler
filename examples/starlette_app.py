"""Minimal Starlette + SQLAlchemy example.

pip install -e . uvicorn
python examples/starlette_app.py
open http://127.0.0.1:8000/books-n1  then  http://127.0.0.1:8000/profiler
"""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from asgi_profiler import install


class Base(DeclarativeBase):
    pass


class Author(Base):
    __tablename__ = "author"
    id = Column(Integer, primary_key=True)
    name = Column(String)


class Book(Base):
    __tablename__ = "book"
    id = Column(Integer, primary_key=True)
    title = Column(String)
    author_id = Column(Integer, ForeignKey("author.id"))
    author = relationship("Author", lazy="select")


engine = create_engine("sqlite:///example.db", connect_args={"check_same_thread": False})
Session = sessionmaker(bind=engine)
Base.metadata.create_all(engine)

with Session() as session:
    if not session.scalars(select(Book)).first():
        for i in range(1, 26):
            session.add(Author(id=i, name=f"Author {i}"))
            session.add(Book(id=i, title=f"Book {i}", author_id=i))
        session.commit()


async def books(request):
    """One query, however many rows."""
    with Session() as session:
        rows = session.scalars(select(Book)).all()
        return JSONResponse([{"id": b.id, "title": b.title} for b in rows])


async def books_n1(request):
    """A deliberate N+1: one query for books, then one per author."""
    with Session() as session:
        out = []
        for book in session.scalars(select(Book)):
            out.append({"title": book.title, "author": book.author.name})
        return JSONResponse(out)


app = Starlette(
    routes=[
        Route("/books", books),
        Route("/books-n1", books_n1),
    ]
)

install(app)  # viewer at /profiler


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
