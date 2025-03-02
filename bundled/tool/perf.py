import cProfile

import deduce


# Rec-desc: 1.574

# lalr: 1.152

cProfile.run("deduce.main(['/home/calvin/Documents/Programming/343/deduce/deduce.py', '/home/calvin/Documents/Programming/343/deduce/lib/Nat.pf', '--dir', '/home/calvin/Documents/Programming/343/deduce/lib', '--dir', '/home/calvin/Documents/Programming/343/deduce/lib'])", sort='cumtime')
