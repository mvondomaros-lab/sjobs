import shutil
import subprocess

import click
import tabulate


@click.command()
def main():
    """Show Slurm jobs."""

    # Define categories, fields, states, and colors.
    common_fields = ["JobID", "Name", "NumCPUS", "tres-per-job"]
    categories = {
        "RUNNING": {
            "fields": common_fields + ["AllocNodes", "EndTime"],
            "states": ["R"],
            "color": "green",
        },
        "PENDING": {
            "fields": common_fields
            + ["Partition", "SubmitTime", "Reason", "Dependency"],
            "states": ["PD"],
            "color": "cyan",
        },
        "OTHER": {
            "fields": common_fields + ["Partition", "State", "Reason"],
            "states": [
                "BF",
                "CA",
                "CD",
                "CF",
                "CG",
                "DL",
                "F",
                "NF",
                "OOM",
                "PR",
                "RD",
                "RF",
                "RH",
                "RQ",
                "RS",
                "RV",
                "SI",
                "SE",
                "SO",
                "ST",
                "S",
                "TO",
            ],
            "color": "red",
        },
    }

    # Build the squeue command line.
    if shutil.which("squeue"):
        cmdline = []
    else:
        cmdline = ["ssh", "marc3a"]
    cmdline += ["squeue", "-me"]

    # Fetch job information.
    for cat, catdict in categories.items():
        # TODO: Dirty fix for tres-per-job format.
        headers = catdict["fields"].copy()
        headers[3] = "TResPerJob"
        format_str = ":500,".join(catdict["fields"]) + ":500"
        state_str = ",".join(catdict["states"])
        color = catdict["color"]

        _cmdline = cmdline + [f"--Format={format_str}", f"--states={state_str}"]
        stdout = subprocess.run(_cmdline, capture_output=True).stdout.decode()
        jobs = [line.split() for line in stdout.splitlines()[1:]]
        if len(jobs) != 0:
            click.echo(click.style(f"\n>>> {cat}", fg=color, bold=True))
            click.echo("\n")
            jobs = tabulate.tabulate(jobs, headers=headers, tablefmt="simple")
            click.echo(jobs)
