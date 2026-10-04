#!/usr/bin/env python3
"""Reference seller tool. One JSON request on stdin, one JSON response on stdout.

This deterministic heuristic is not an LLM and is not a complete solver. An AI
agent can call it or submit another schedule through the same checker.
"""
import json
import sys


def solve(instance):
    machines, jobs = instance["machines"], instance["jobs"]
    if not 0 < len(jobs) <= 128 or not 0 < len(machines) <= 16:
        raise ValueError("Instance exceeds reference solver limits")
    schedule, ends, total = [], [], 0
    for index, job in enumerate(jobs):
        if any(dep < 0 or dep >= index for dep in job["dependencies"]):
            raise ValueError("Jobs must be topologically ordered")
        release = max([job["release"]] + [ends[dep] for dep in job["dependencies"]])
        choices = []
        for machine_id, machine in enumerate(machines):
            duration = job["durations"][machine_id]
            if duration <= 0 or job["units"] > machine["capacity"]:
                continue
            starts = {release} | {end for end in ends if end >= release}
            for start in sorted(starts):
                end = start + duration
                if end > job["deadline"]:
                    continue
                # Check the new start and every existing start inside its interval.
                events = {start} | {a["start"] for a in schedule if start <= a["start"] < end}
                fits = all(job["units"] + sum(
                    jobs[k]["units"] for k, a in enumerate(schedule)
                    if a["machine"] == machine_id and a["start"] <= t < ends[k]
                ) <= machine["capacity"] for t in events)
                if fits:
                    cost = duration * job["units"] * machine["price_per_unit_tick"]
                    choices.append((cost, end, machine_id, start))
                    break
        if not choices:
            raise ValueError(f"Heuristic found no feasible placement for job {index}")
        cost, end, machine_id, start = min(choices)
        schedule.append(dict(job=index, machine=machine_id, start=start))
        ends.append(end)
        total += cost
    return schedule, total


def main():
    raw = sys.stdin.buffer.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Request exceeds 1 MiB")
    request = json.loads(raw)
    if request.get("protocol") != "warrant.solver.v1":
        raise ValueError("Unknown protocol")
    schedule, cost = solve(request["instance"])
    if cost > request["maxCost"]:
        raise ValueError("No qualifying quote from this heuristic")
    kind = request["type"]
    if kind == "rfq":
        response = dict(type="quote", instanceHash=request["instanceHash"],
                        amount=request["amount"], computedCost=cost,
                        seller=request["seller"], checkerVersion=1)
    elif kind == "work":
        response = dict(type="delivery", taskId=request["taskId"], schedule=schedule)
    else:
        raise ValueError("Expected rfq or work")
    print(json.dumps(dict(protocol="warrant.solver.v1", **response)))


if __name__ == "__main__":
    try:
        main()
    except (KeyError, TypeError, ValueError, IndexError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
