import argparse
import os

from .render import ChartRenderer, load_pjsk, load_sus


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "score", metavar="<xxx.sus|xxx.json>", help="the pjsekai score file"
    )
    parser.add_argument("--title")
    parser.add_argument("--artist")
    parser.add_argument("--difficulty")
    parser.add_argument("--playlevel")
    parser.add_argument("--jacket", help="path or url of the jacket image")
    parser.add_argument("-o", "--output", metavar="<xxx.png>")
    args = parser.parse_args()

    input_path = os.path.abspath(args.score)
    output = args.output or os.path.splitext(input_path)[0] + ".png"
    if os.path.isdir(output):
        output = os.path.join(
            output, os.path.splitext(os.path.basename(input_path))[0] + ".png"
        )

    if input_path.lower().endswith(".json"):
        score, bar_lengths = load_pjsk(input_path)
    else:
        score, bar_lengths = load_sus(input_path)
    renderer = ChartRenderer(
        score,
        title=args.title,
        artist=args.artist,
        difficulty=args.difficulty,
        playlevel=args.playlevel,
        jacket=args.jacket,
        bar_lengths=bar_lengths,
    )
    renderer.render().save(output)


if __name__ == "__main__":
    main()
