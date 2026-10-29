### Fixed

- A fresh verdict on a Doctor CI speed finding no longer lowers its return
  bar: a judged scope stays silent until a run passes the highest worst judged
  on that scope within the last day, plus the growth margin. Each re-judged
  verdict keeps its earlier verdicts' time and worst.
