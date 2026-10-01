"""Reviewed, repository-owned skills explicitly mounted into the meal workflow."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'skills'
MEAL_SKILLS = ('mymiam-meal-capture', 'mymiam-food-catalogue')


def meal_instructions():
    sections = []
    for name in MEAL_SKILLS:
        source = (ROOT / name / 'SKILL.md').read_text(encoding='utf-8')
        if not source.startswith('---\n'):
            raise ValueError('Skill MyMiam sans métadonnées : ' + name)
        _, metadata, body = source.split('---', 2)
        sections.append(f'<skill name="{name}">\n{body.strip()}\n</skill>')
    return '\n\n'.join(sections)
