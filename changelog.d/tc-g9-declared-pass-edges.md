### Changed

- Every tick step that stores a value in the pass box declares it as a pass write, and every step that reads one declares the read and a need on its writer, so the registry refuses a pass box hand off between unordered steps. A recording test replays the twelve tick scenarios and names any undeclared read, missing need or undeclared write.
