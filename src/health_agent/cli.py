import typer

app = typer.Typer(help="Personal Health Agent")


@app.callback()
def health_agent() -> None:
    """Personal Health Agent."""


def main() -> None:
    app()
