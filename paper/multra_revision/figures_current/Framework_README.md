# Framework typography

`Framework_updated.pdf` combines an image-generated background with editable vector Arial Bold labels. Only lettering is vector; icons and artwork remain raster. The current background uses flat outlined icons and white panels. The entire candidate-budget footer has been removed; budget accounting remains in the manuscript methods. The original framework is preserved separately.

To rebuild, install Arial (regular and bold), then from this directory run:

```bash
lualatex -interaction=nonstopmode -halt-on-error Framework_updated.tex
pdftoppm -scale-to 3344 -png -singlefile Framework_updated.pdf Framework_updated
```

Edit wording and label coordinates in `Framework_typography.tex`. Label coordinates refer to the top of the original 1672 × 941 bp canvas. The output page is 1672 × 841 bp, omitting the empty former footer. Main manuscript compilation uses the prebuilt PDF and does not require LuaLaTeX or Arial.
