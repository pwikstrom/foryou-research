
# Shared content for Home and Login pages
#
# HOME_CONTENT structure:
#   hero_title  — main heading
#   intro       — introductory paragraph (supports HTML via |safe)
#   sections    — list of content blocks rendered in order
#
# Each section dict:
#   title       — section heading
#   paragraphs  — list of text items; each item is either:
#       str  → rendered as a normal prose paragraph
#       dict → must contain 'text'; optional 'style' key:
#              'callout' → rendered inside a note-callout box

HOME_CONTENT = {
    'hero_title': "The For You Data Hub",
    'intro': (
        "Welcome to the For You Data Hub. We invite you to experiment with this research tool "
        "and explore data from the For You Project. Read more about the project "
        "<a href='https://www.foryouresearch.net/' target='_blank'>here</a>."
    ),
    'sections': [
        {
            'title': "Important note",
            'paragraphs': [
                {
                    'text': (
                        "This tool is currently under development, so you may occasionally encounter bugs "
                        "or unexpected behaviour. If you do, we’d really appreciate your help in improving "
                        "it—please feel free to let us know at info@foryouresearch.net."
                    ),
                    'style': 'callout'
                },
                {
                    'text': (
                        "Some of the data and insights presented here are generated using AI. While we aim "
                        "for accuracy, there may be instances where outputs are imperfect or unexpected. "
                        "We appreciate your understanding as we continue to refine both the tool and our "
                        "research methods."
                    ),
                    'style': 'callout'
                },
            ]
        },
        {
            'title': "Acknowledgements",
            'paragraphs': [
                (
                    "This research project is based at QUT's Digital Media Research Centre in Brisbane and at "
                    "the University of Sydney. Our work is supported by a grant from the Australian Research Council "
                    "and by the Australian Internet Observatory (AIO). The AIO received investment from the "
                    "Australian Research Data Commons through the National Collaborative Infrastructure Strategy "
                    "in partnership with a consortium of Australian universities. TikTok is a registered "
                    "trademark of Bytedance Ltd. This website is not affiliated with or endorsed by TikTok "
                    "or Bytedance Ltd."
                ),
            ]
        },
    ]
}
