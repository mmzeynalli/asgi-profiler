"""FastAPI + SQLModel example.

    pip install -e . fastapi sqlmodel uvicorn
    python examples/fastapi_sqlmodel_app.py
    open http://127.0.0.1:8000/books-n1  then  http://127.0.0.1:8000/profiler

SQLModel needs no special handling: `create_engine` returns a SQLAlchemy
`Engine` and `Session` subclasses `sqlalchemy.orm.Session`, and the profiler
listens on the `Engine` class, so the same cursor events fire.
"""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import text
from sqlmodel import Field, Relationship, Session, SQLModel, create_engine, select

from asgi_profiler import install


class Author(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    # UP037 quoted-annotation: SQLModel resolves the annotation at runtime,
    # so the quotes have to stay.
    books: list['Book'] = Relationship(back_populates='author')  # noqa: UP037


class Book(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    author_id: int | None = Field(default=None, foreign_key='author.id')
    author: Author | None = Relationship(back_populates='books')


engine = create_engine('sqlite:///example.db', connect_args={'check_same_thread': False})
SQLModel.metadata.create_all(engine)

with Session(engine) as session:
    if not session.exec(select(Book)).first():
        for i in range(1, 26):
            session.add(Author(id=i, name=f'Author {i}'))
            session.add(Book(id=i, title=f'Book {i}', author_id=i))
        session.commit()


app = FastAPI()


@app.get('/books')
def list_books():
    """One query, however many rows."""
    with Session(engine) as session:
        return [{'id': b.id, 'title': b.title} for b in session.exec(select(Book))]


@app.get('/books-n1')
def list_books_n1():
    """A deliberate N+1: one query for books, then one per author."""
    with Session(engine) as session:
        return [{'title': b.title, 'author': b.author.name} for b in session.exec(select(Book))]


@app.get('/books/{book_id}')
def get_book(book_id: int):
    """Hit this with a few different ids.

    The request list shows each concrete path; the summary page groups them all
    under `/books/{book_id}`, which is the number you actually want.
    """
    with Session(engine) as session:
        book = session.get(Book, book_id)
        return {'id': book.id, 'title': book.title} if book else {}


@app.get('/broken')
def broken():
    """A statement that raises. It shows up in the trace, marked failed."""
    with Session(engine) as session:
        try:
            session.exec(text('SELECT * FROM no_such_table'))
        except Exception as exc:
            session.rollback()
            return {'caught': type(exc).__name__}
    return {}


# Swap in SQLiteStorage to keep history across restarts and across workers:
#     from asgi_profiler import SQLiteStorage
#     install(app, storage=SQLiteStorage("profiler.db"))
install(app)  # viewer at /profiler


if __name__ == '__main__':
    import uvicorn

    uvicorn.run(app, host='127.0.0.1', port=8000)
