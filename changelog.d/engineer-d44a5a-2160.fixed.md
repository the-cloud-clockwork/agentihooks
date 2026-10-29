Keep Doctor detection outside the controller tick deadline by running it in the detached refresh worker. Reuse recent published health findings to avoid duplicate detector work.
