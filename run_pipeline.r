#!/usr/bin/env Rscript
# ============================================================
# run_pipeline.r  --  SIA Preparedness Validation pipeline
# Author: Gorgui Ba Toure
#
# Same layout as the team supervision / LQAS / IM pipelines. Run from the project folder:
#     Rscript run_pipeline.r
#
# Steps
#   1 fetch     python fetch_ona_data.py        ONA download (token per row of config/forms.csv) + the form definitions
#   2 inspect   python tools/inspect_forms.py   questions of every form, share filled, templates  -> reports/
#   (3 process, 4 clean, 5 qc, 6 sharepoint: added once the schema report has been read)
#
# Options
#   --skip-fetch --skip-inspect
#   --countries AGO[,ETH,...]     only these ISO3 codes in the fetch step
#   --allow-shrink                accept a download with >10% fewer records than before
# ============================================================
args <- commandArgs(trailingOnly = TRUE)
has <- function(flag) flag %in% args
opt_value <- function(flag) { i <- match(flag, args); if (is.na(i) || i == length(args)) NULL else args[i + 1] }
py <- Sys.which("python"); if (py == "") py <- Sys.which("python3")
if (py == "") stop("python was not found on the PATH")
fetch_args <- c("fetch_ona_data.py")
if (!is.null(opt_value("--countries"))) fetch_args <- c(fetch_args, "--countries", opt_value("--countries"))
if (has("--allow-shrink")) fetch_args <- c(fetch_args, "--allow-shrink")
steps <- list(
  list(name = "Fetch from ONA",     skip = has("--skip-fetch"),   cmd = py, args = fetch_args,                  hard_stop = FALSE),
  list(name = "Inspect the forms",  skip = has("--skip-inspect"), cmd = py, args = c("tools/inspect_forms.py"), hard_stop = FALSE)
)
cat(strrep("=", 60), "\nSIA Preparedness Validation pipeline  --  ", format(Sys.time(), "%Y-%m-%d %H:%M:%S"), "\n", strrep("=", 60), "\n", sep = "")
outcome <- list()
for (s in steps) {
  if (isTRUE(s$skip)) { cat("\n>>> SKIPPED: ", s$name, "\n", sep = ""); outcome[[s$name]] <- "skipped"; next }
  cat("\n>>> ", s$name, "\n", sep = "")
  status <- suppressWarnings(system2(s$cmd, s$args))
  ok <- identical(as.integer(status), 0L)
  cat(sprintf("<<< %s: %s\n", s$name, if (ok) "OK" else "FAILED"))
  outcome[[s$name]] <- if (ok) "ok" else "failed"
}
cat("\n", strrep("=", 60), "\nSummary\n", sep = "")
for (n in names(outcome)) cat(sprintf("  %-28s %s\n", n, outcome[[n]]))
quit(status = ifelse(any(unlist(outcome) == "failed"), 1, 0))
