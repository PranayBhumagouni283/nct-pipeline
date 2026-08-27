"""
This script is no longer needed.

The combined_pipeline.py now runs ALL pipelines (asset + all indications)
in a single CT.gov fetch by default:

    python combined_pipeline.py ADC     # runs asset + all ADC indications
    python combined_pipeline.py ASMB    # runs asset + all ASMB indications

For a single indication only (debugging):
    python combined_pipeline.py ADC --indication "Ovarian Cancer"
"""
print(__doc__)
