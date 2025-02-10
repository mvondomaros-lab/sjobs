import shutil
import subprocess

import click
import tabulate


@click.command()
def main():
    """Show Slurm jobs."""

    # Define categories, fields, states, and colors.
    categories = {
        "Running": {
            "fields": [
                "JobID",
                "Name",
                "NumCPUs",
                "cpus-per-tres",
                "AllocNodes",
                "EndTime",
            ],
            "states": ["R"],
            "color": "green",
        },
        "Pending": {
            "fields": [
                "JobID",
                "Name",
                "NumCPUs",
                "cpus-per-tres",
                "Partition",
                "SubmitTime",
                "Reason",
                "Dependency",
            ],
            "states": ["PD"],
            "color": "cyan",
        },
        "Other": {
            "fields": [
                "JobID",
                "Name",
                "NumCPUs",
                "cpus-per-tres",
                "State",
                "Reason",
            ],
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
        cmdline = ["squeue", "--me"]
    else:
        cmdline = ["ssh", "marc3a", "squeue", "--me"]

    # Fetch job information.
    for cat, catdict in categories.items():
        format_str = ":500,".join(catdict["fields"]) + ":500"
        state_str = ",".join(catdict["states"])
        color = catdict["color"]

        stdout = subprocess.run(
            cmdline + [f"--Format='{format_str}'", f"--states='{state_str}'"],
            capture_output=True,
        ).stdout.decode()
        jobs = [line.split() for line in stdout.splitlines()]
        if len(jobs) != 1:
            click.echo(click.style(f"[{cat}]", fg=color, bold=True))
            click.echo("\n")
            jobs = tabulate.tabulate(jobs, headers="firstrow")
            click.echo(jobs)
            click.echo("\n")
