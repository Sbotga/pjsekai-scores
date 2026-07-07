# Project Sekai Scores

Renders PJSK music scores to chart images (PNG). Takes a
[sonolus-level-converters](https://github.com/UntitledCharts/sonolus-level-converters)
`Score` object as input, with loaders included for `.sus` files and pjsk server
json.

## Installation

Install with pip：

```
pip install git+https://github.com/Sbotga/pjsekai-scores
```

## Usage

Render a score file from the command line：

```
python -m sekaiworld.scores <xxx.sus> [--title ...] [--artist ...] [--difficulty ...] [--playlevel ...] [--jacket <path or url>] [-o <xxx.png>]
```

pjsk json score files are detected by their `.json` extension.

Here is an example of using it as a package to generate a chart image:

```python
from sekaiworld.scores import ChartRenderer, load_sus, load_pjsk

score, bar_lengths = load_sus('1.sus')  # or load_pjsk('1.json')

renderer = ChartRenderer(
    score,                # a sonolus_converters Score
    title='Tell Your World',
    difficulty='master',
    jacket='jacket.png',  # path or http(s) url, optional
    bar_lengths=bar_lengths,
)
renderer.render().save('1.png')  # render() returns a PIL.Image
```

Any `sonolus_converters` `Score` works as input. Only the note types the chart
view draws are supported: BPM changes, time scale changes (drawn as speed
lines), singles, slides, guides, and skill/fever markers. `bar_lengths` carries
the time signatures (`load_sus` reads them from the file; pjsk json has none,
so those charts render as 4/4).

## License

Project Sekai Scores is licensed under the MIT License. See the [LICENSE](LICENSE) file for more information.

Project Sekai Scores is in no way affiliated with SEGA, Colorful Palette, or Project Sekai.
