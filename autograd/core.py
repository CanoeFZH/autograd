from itertools import count
from functools import reduce
from .tracer import trace, primitive, toposort, Node, Box, isbox, getval
from .util import func, subval, subvals

# -------------------- reverse mode --------------------

def make_vjp(fun, x):
    start_node = VJPNode.new_root()
    end_value, end_node =  trace(start_node, fun, x)
    if end_node is None:
        def vjp(g): return vspace(x).zeros()
    else:
        def vjp(g): return backward_pass(g, end_node)
    return vjp, end_value

def backward_pass(g, end_node):
    outgrads = {end_node : (g, False)}
    for node in toposort(end_node, get_parent_list):
        outgrad = outgrads.pop(node)
        def accumulate(parent, parent_outgrad):
            if parent:
                outgrads[parent] = add_outgrads(outgrads.get(parent), parent_outgrad)
        node.fmap(accumulate, node.parents, node.vjp(outgrad[0]))

    return outgrad[0]

def get_parent_list(node):
    parents = []
    node.fmap(lambda p: p and parents.append(p), node.parents)
    return parents

class VJPNode(Node):
    __slots__ = ['parents', 'vjp', 'fmap']
    def __init__(self, value, fun, args, kwargs, parents, fmap):
        self.parents = parents
        self.fmap = fmap
        try:
            vjpmaker = primitive_vjps[fun]
        except KeyError:
            fun_name = getattr(fun, '__name__', fun)
            raise NotImplementedError("VJP of {} not defined".format(fun_name))
        self.vjp = vjpmaker(parents, value, args, kwargs)

    def initialize_root(self):
        self.parents = []
        self.fmap = map
        self.vjp = lambda g: ()

primitive_vjps = {}
def defvjp_full(fun, vjpmaker):
    primitive_vjps[fun] = vjpmaker

def defvjp_argnum(fun, vjpmaker):
    assert fun._fmap is map
    def vjp_full(parents, *args):
        vjps = [parent and vjpmaker(argnum, *args)
                for argnum, parent in enumerate(parents)]
        return lambda g: (vjp and vjp(g) for vjp in vjps)
    defvjp_full(fun, vjp_full)

def defvjp(fun, *vjpmakers):
    def vjp_full(parents, ans, args, kwargs):
        vjps = fun._fmap(lambda p, vjpmaker:
                         p and vjpmaker(ans, *args, **kwargs), parents, vjpmakers)
        return lambda g: fun._fmap(lambda p, vjp: p and vjp(g), parents, vjps)

    defvjp_full(fun, vjp_full)

# -------------------- forward mode --------------------

def make_jvp(fun, x):
    def jvp(g):
        start_node = JVPNode.new_root(g)
        end_value, end_node = trace(start_node, fun, x)
        if end_node is None:
            return end_value, vspace(end_value).zeros()
        else:
            return end_value, end_node.g
    return jvp

class JVPNode(Node):
    __slots__ = ['g']
    def __init__(self, value, fun, args, kwargs, parents, fmap):
        parent_gs = fmap(lambda p: p and p.g, parents)
        try:
            jvpmaker = primitive_jvps[fun]
        except KeyError:
            name = getattr(fun, '__name__', fun)
            raise NotImplementedError("JVP of {} wrt".format(name))
        self.g = jvpmaker(parents, parent_gs, value, args, kwargs)

    def initialize_root(self, g):
        self.g = g

primitive_jvps = {}
def defjvp_full(fun, jvpmaker):
    primitive_jvps[fun] = jvpmaker

def defjvp_argnum(fun, jvpmaker):
    assert fun._fmap is map
    def jvp_full(parents, gs, ans, args, kwargs):
        return sum_outgrads(jvpmaker(argnum, g, ans, args, kwargs)
                            for argnum, parent, g in zip(count(), parents, gs) if parent)
    defjvp_full(fun, jvp_full)

def defjvp(fun, *jvpfuns):
    assert fun._fmap is map
    def jvp_full(parents, gs, ans, args, kwargs):
        return sum_outgrads(
            translate_jvp(jvpfun, fun, argnum)(g, ans, *args, **kwargs)
            for argnum, g, jvpfun, parent in
            zip(count(), gs, jvpfuns, parents) if parent)

    defjvp_full(fun, jvp_full)

def translate_jvp(jvpfun, fun, argnum):
    if jvpfun == 'same':
        return (lambda g, ans, *args, **kwargs:
                fun(*subval(args, argnum, g), **kwargs))
    elif callable(jvpfun):
        return jvpfun
    else:
        raise Exception("Bad JVP '{}' for '{}'".format(jvpfun, fun.__name__))

def def_linear(fun):
    """Flags that a function is linear wrt all args"""
    defjvp_argnum(fun, lambda argnum, g, ans, args, kwargs:
                  fun(*subval(args, argnum, g), **kwargs))

# -------------------- vector behavior --------------------

def add_outgrads(prev_g_flagged, g):
    sparse = type(g) in sparse_object_types
    if prev_g_flagged:
        vs = vspace(g)
        prev_g, mutable = prev_g_flagged
        if mutable:
            if sparse:
                return sparse_add(vs, prev_g, g), True
            else:
                return vs.mut_add(prev_g, g), True
        else:
            if sparse:
                prev_g_mutable = vs.mut_add(None, prev_g)
                return sparse_add(vs, prev_g_mutable, g), True
            else:
                return vs.add(prev_g, g), True
    else:
        if sparse:
            return sparse_add(vspace(g), None, g), True
        else:
            return g, False

def sum_outgrads(gs):
    return reduce(add_outgrads, gs, None)[0]

@primitive
def sparse_add(vs, x_prev, x_new):
    x_prev = x_prev if x_prev is not None else vs.zeros()
    return x_new.mut_add(x_prev)

class VSpace(object):
    __slots__ = []
    mappings = {}
    iscomplex = False
    def __init__(self, value): pass

    def zeros(self):          assert False, repr(self)
    def ones(self):           assert False, repr(self)
    def standard_basis(self): assert False, repr(self)
    def randn(self):          assert False, repr(self)

    @primitive
    def mut_add(self, x_prev, x_new):
      x_prev = x_prev if x_prev is not None else self.zeros()
      return self._mut_add(x_prev, x_new)
    @primitive
    def add(self, x_prev, x_new):     return self._add(x_prev, x_new)
    @primitive
    def scalar_mul(self, x, a):       return self._scalar_mul(x, a)
    @primitive
    def inner_prod(self, x, y):       return self._inner_prod(x, y)
    @primitive
    def covector(self, x):            return self._covector(x)

    def _add(self, x, y):        return x + y
    def _mut_add(self, x, y):    x += y; return x
    def _scalar_mul(self, x, a): return x * a
    def _inner_prod(self, x, y): assert False
    def _covector(self, x):      return x

    def __eq__(self, other):
        return type(self) == type(other) and self.__dict__ == other.__dict__

    def __repr__(self):
        return "{}_{}".format(type(self).__name__, self.__dict__)

    @classmethod
    def register(cls, value_type, vspace_maker=None):
        if vspace_maker:
            VSpace.mappings[value_type] = vspace_maker
        else:
            VSpace.mappings[value_type] = cls

def vspace(value):
    try:
        return VSpace.mappings[type(value)](value)
    except KeyError:
        if isbox(value):
            return vspace(getval(value))
        else:
            raise TypeError("Can't find vector space for value {} of type {}. "
                            "Valid types are {}".format(
                                value, type(value), VSpace.mappings.keys()))

class SparseBox(Box):
    __slots__ = []
class SparseObject(object):
    __slots__ = ['vs', 'mut_add']
    def __init__(self, vs, mut_add):
        self.vs = vs
        self.mut_add = mut_add
VSpace.register(SparseObject, lambda x : x.vs)
SparseBox.register(SparseObject)
sparse_object_types = {SparseObject, SparseBox}

# -------------------- core reverse mode grads --------------------

identity_vjp = lambda argnums, *args: lambda g: g
defvjp(sparse_add, None, identity_vjp, identity_vjp)
defvjp(func(VSpace.add    ), None, identity_vjp, identity_vjp)
defvjp(func(VSpace.mut_add), None, identity_vjp, identity_vjp)
defvjp(func(VSpace.inner_prod), None,
       lambda ans, vs, x, y: lambda g:  vs.covector(vs.scalar_mul(y, g)),
       lambda ans, vs, x, y: lambda g:  vs.covector(vs.scalar_mul(x, g)))
defvjp(func(VSpace.covector), None,
          lambda ans, vs, x: lambda g: vs.covector(g))
defvjp(func(VSpace.scalar_mul), None,
       lambda ans, vs, x, a: lambda g: vs.covector(vs.scalar_mul(vs.covector(g), a)),
       lambda ans, vs, x, a: lambda g: vs.inner_prod(g, vs.covector(x)))

# -------------------- core forward mode grads --------------------

identity_jvp = lambda g, *args, **kwargs: g
defjvp(sparse_add, None, identity_jvp, identity_jvp)
defjvp(func(VSpace.mut_add), None, identity_jvp, identity_jvp)
defjvp(func(VSpace.add),     None, identity_jvp, identity_jvp)
defjvp(func(VSpace.scalar_mul), None, 'same', 'same')
defjvp(func(VSpace.inner_prod), None, 'same', 'same')
defjvp(func(VSpace.covector),   None, 'same')

# -------------------- deprecation warnings -----------------------

import warnings
deprecated_defvjp_message = '''
The {} method is deprecated. See the update guide and tutorial:
https://github.com/HIPS/autograd/blob/master/docs/updateguide.md
https://github.com/HIPS/autograd/blob/master/docs/tutorial.md'''

def deprecated_defvjp(primitive_fun):
    deprecation_msg = deprecated_defvjp_message.format('defvjp')
    vjpfuns = {}
    def defvjp_unstaged(vjpmaker, argnum=0):
        warnings.warn(deprecation_msg)
        def staged_vjpmaker(ans, *args, **kwargs):
            def vjp(g):
                vs, gvs = vspace(args[argnum]), vspace(g)
                return vjpmaker(g, ans, vs, gvs, *args, **kwargs)
            return vjp
        vjpfuns[argnum] = staged_vjpmaker
        argnums, vjpmakers = zip(*[(argnum, vjpfuns[argnum])
                                   for argnum in sorted(vjpfuns.keys())])
        defvjp(primitive_fun, *vjpmakers, argnums=argnums)
    return defvjp_unstaged

def deprecated_defvjp_is_zero(primitive_fun):
    deprecation_msg = deprecated_defvjp_message.format('defvjp_is_zero')
    zero_vjps = [set()]
    def defvjp_is_zero(argnums=(0,)):
        warnings.warn(deprecation_msg)
        zero_vjps[0] |= set(argnums)
        nones = [None] * len(zero_vjps[0])
        defvjp(primitive_fun, *nones, argnums=sorted(zero_vjps[0]))
    return defvjp_is_zero

def deprecated_defgrad(primitive_fun):
    deprecation_msg = deprecated_defvjp_message.format('defgrad')
    gradfuns = {}
    def defgrad(gradfun, argnum=0):
        warnings.warn(deprecation_msg)
        gradfuns[argnum] = gradfun
        argnums, vjpmakers = zip(*[(argnum, gradfuns[argnum])
                                   for argnum in sorted(gradfuns.keys())])
        defvjp(primitive_fun, *vjpmakers, argnums=argnums)
    return defgrad

primitive_ = primitive

def primitive_with_deprecation_warnings(f_raw):
    f_wrapped = primitive_(f_raw)
    f_wrapped.defvjp = deprecated_defvjp(f_wrapped)
    f_wrapped.defvjp_is_zero = deprecated_defvjp_is_zero(f_wrapped)
    f_wrapped.defgrad = deprecated_defgrad(f_wrapped)
    return f_wrapped

primitive = primitive_with_deprecation_warnings
