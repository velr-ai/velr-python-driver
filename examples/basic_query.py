"""Run a parameterized query and read rows through the public driver API."""

from velr.driver import Velr


def main():
    with Velr.open() as db:
        db.run(
            "CREATE (:Movie {title:$title, released:$year, genres:$genres})",
            params={"title": "Inception", "year": 2010, "genres": ["Sci-Fi", "Heist"]},
        )
        with db.exec_one(
            "MATCH (m:Movie {title:$title}) "
            "RETURN m.title AS title, m.released AS year, m.genres AS genres",
            params={"title": "Inception"},
        ) as table:
            print("columns:", table.column_names())
            with table.iter_rows() as rows:
                for row in rows:
                    print([cell.as_python(parse_json=True) for cell in row])


if __name__ == "__main__":
    main()
