declare module 'plotly.js-cartesian-dist-min' {
  // The cartesian build exposes the same surface as plotly.js at runtime
  // for the trace types we use (scatter/bar/heatmap/histogram/box/violin/
  // contour/pie). We re-use the @types/plotly.js declarations.
  import * as Plotly from 'plotly.js'
  export = Plotly
}
