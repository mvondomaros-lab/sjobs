import io
import os
import subprocess

import click
import pandas as pd


@click.command()
def main():
    """Show Slurm jobs."""

    # Define fields, their format codes, and their order.
    fieldcodes = {
        "Name": "%j",
        "JobID": "%i",
        "State": "%t",
        "Reason": "%r",
        "Nodes": "%N",
        "TimeLeft": "%M",
    }
    delimiter = "|"

    # Fetch the squeue output.
    stdout = subprocess.run(
        [
            "squeue",
            "-u",
            os.getlogin(),
            f"--format='{delimiter.join(fieldcodes.values())}'",
        ],
        capture_output=True,
    ).stdout.decode()

    # Parse the squeue output.
    df = pd.read_csv(
        io.StringIO(stdout), sep=delimiter, skiprows=1, names=fieldcodes.keys()
    ).sort_values(by="Name")

    # Output
    click.echo(df.to_string(index=False))
