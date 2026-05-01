"""Vote aggregation for multi-agent consultant system."""


def aggregate_votes(signal, votes: dict) -> bool:
    """
    Return True if the signal should proceed after consultant votes.
    Rules:
    - No consultants → always proceed
    - Majority AGREE or ABSTAIN → proceed
    - Majority DISAGREE → block
    """
    if not votes:
        return True

    agree = sum(1 for v in votes.values() if v["vote"] == "AGREE")
    disagree = sum(1 for v in votes.values() if v["vote"] == "DISAGREE")

    # Block only if more disagree than agree
    return disagree <= agree
