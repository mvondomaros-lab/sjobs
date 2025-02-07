import json
import os
import shutil
import subprocess

import click
import pandas as pd


@click.command()
def main():
    """Show Slurm jobs."""

    # Get the username.
    user = os.getenv("USER")

    # Parse the squeue output.
    stdout = subprocess.run(["squeue", "--json"], capture_output=True).stdout
    jobs = json.loads(stdout).setdefault("jobs", [])
    jobs = pd.DataFrame(jobs)

    # Filter jobs by user.
    jobs = jobs[jobs["user_name"] == user]

    # Covert "tres_per_job" to "gpus".
    jobs["gpus"] = jobs["tres_per_job"].apply(_tres_per_job_to_gpus)

    # Select output columns and sort by name.
    jobs = jobs[
        [
            "name",
            "job_id",
            "cpus",
            "gpus",
            "partition",
            "job_state",
            "state_reason",
            "dependency",
        ]
    ].sort_values(by=["name"])

    # Output the formatted dataframe.
    click.echo(jobs.to_string(index=False))


def _tres_per_job_to_gpus(s: str) -> int:
    split = s.split(":")
    if split[0] == "gres" and split[1] == "gpu":
        return int(split[2])
    else:
        return 0
