declare module 'plotly.js-dist-min' {
  // The dist-min build exposes the same surface as plotly.js at runtime.
  // We re-use the @types/plotly.js declarations.
  import * as Plotly from 'plotly.js'
  export = Plotly
}
