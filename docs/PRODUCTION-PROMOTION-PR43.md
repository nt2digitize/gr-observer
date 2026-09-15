# Production promotion — PR #43

This documentation-only commit records the production promotion trigger for PR #43 after CI passed.

Runtime behavior promoted: group visibility reposts and reactive replies share one managed visible-message slot per group. The prior managed message is deleted only by its recorded Telegram message ID; no history scan or third-party message deletion is introduced.

Source merge: d4f75d15bfa53799e19c46f95a0ecc38063d6875
