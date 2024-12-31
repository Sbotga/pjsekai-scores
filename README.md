# Project Sekai Scores

Analyzes PJSK music score files (.sus) and generates vector images (SVG).

## Installation

Install with pip：

```
pip install git+https://github.com/Sbogta/pjsekai-scores
```

Build and install manually：

```
python -m build
pip install dist/sekaiworld.scores-*.whl
```

## Usage

Project Sekai Scores includes a default script that can load a local SUS score file and convert it to an SVG vector image. This can be done with the following command：

```
python -m sekaiworld.scores <xxx.sus>
```

Here is an example of using it as a package to generate a chart image:

```python
import sekaiworld.scores

score = sekaiworld.scores.Score.open('1.sus', encoding='UTF-8')
drawing = sekaiworld.scores.Drawing(score=score)
drawing.svg().saveas('1.svg')
```

We provide customization features to enrich and personalize your music score files. Please refer to the following documents:

* [Convert BPM, Time Signature, Section](https://gitlab.com/pjsekai/scores/-/wikis/rebase)

* [Add lyrics](https://gitlab.com/pjsekai/scores/-/wikis/lyric)

* [Customize your stylesheet](https://gitlab.com/pjsekai/scores/-/wikis/css)

## License

Project Sekai Scores is licensed under the MIT License. See the [LICENSE](LICENSE) file for more information.

Project Sekai Scores is in no way affiliated with SEGA, Colorful Palette, or Project Sekai.
