import ast
from tracer import decltype, is_list, is_builtin_import, is_recursive
from clike import CLikeTranspiler
from context import (add_scope_context, add_variable_context,
                     add_list_calls, add_imports)


def transpile(source):
    tree = ast.parse(source)
    add_variable_context(tree)
    add_scope_context(tree)
    add_list_calls(tree)
    add_imports(tree)
    cpp = CppTranspiler().visit(tree)
    return cpp


class CppTranspiler(CLikeTranspiler):
    def __init__(self):
        self._function_stack = []
        self._vars = set()

    def visit_FunctionDef(self, node):
        self._function_stack.append(node)

        def template_fun():
            args = []
            for idx, arg in enumerate(node.args.args):
                args.append(("T" + str(idx + 1), arg.id))
            typenames = ["typename " + arg[0] for arg in args]
            template = "template <{0}>".format(", ".join(typenames))
            params = ["{0} {1}".format(arg[0], arg[1]) for arg in args]
            funcdef = "{0}\nauto {1}({2})".format(template, node.name, ", ".join(params))
            return funcdef

        def lambda_fun():
            params = ["auto {0}".format(param.id) for param in node.args.args]
            funcdef = "auto {0} = []({1})".format(node.name, ", ".join(params))
            return funcdef

        body = [self.visit(child) for child in node.body]
        body = " {\n" + "\n".join(body) + "\n}"

        self._function_stack.pop()

        if is_recursive(node):
            return template_fun() + body
        else:
            return lambda_fun() + body + ";"

    def visit_Attribute(self, node):
        attr = node.attr
        if is_builtin_import(node.value.id):
            return "py14::" + node.value.id + "::" + attr
        elif node.value.id == "math":
            if node.attr == "asin":
                return "std::asin"
            elif node.attr == "atan":
                return "std::atan"
            elif node.attr == "acos":
                return "std::acos"

        if is_list(node.value):
            if node.attr == "append":
                attr = "push_back"
        return node.value.id + "." + attr

    def visit_Call(self, node):
        fname = self.visit(node.func)
        if node.args:
            args = [self.visit(a) for a in node.args]
            args = ", ".join(args)
        else:
            args = ''

        if fname == "int":
            return "py14::to_int({0})".format(args)
        elif fname == "str":
            return "std::to_string({0})".format(args)
        elif fname == "max":
            return "std::max({0})".format(args)
        elif fname == "range" or fname == "xrange":
            return "py14::range({0})".format(args)
        elif fname == "len":
            return "py14::len({0})".format(args)


        return '{0}({1})'.format(fname, args)

    def visit_For(self, node):
        target = self.visit(node.target)
        iter = self.visit(node.iter)
        buffer = []
        buffer.append('for(auto {0} : {1}) {{'.format(target, iter))
        buffer.extend([self.visit(c) for c in node.body])
        buffer.append("}")
        return "\n".join(buffer)

    def visit_Expr(self, node):
        s = self.visit(node.value)
        if s.strip() and not s.endswith(';'):
            s += ';'
        if s == ';':
            return ''
        else:
            return s

    def visit_Str(self, node):
        """Use a C++ 14 string literal instead of raw string"""
        return super(CppTranspiler, self).visit_Str(node) + "s"

    def visit_Name(self, node):
        if node.id == 'None':
            return 'nullptr'
        else:
            return super(CppTranspiler, self).visit_Name(node)

    def visit_If(self, node):
        if self.visit(node.test) == '__name__ == "__main__"s':
            buffer = ["int main(int argc, char ** argv) {",
                      "py14::sys::argv = " \
                      "std::vector<std::string>(argv, argv + argc);"]
            buffer.extend([self.visit(child) for child in node.body])
            buffer.append("}")
            return "\n".join(buffer)

        else:
            return super(CppTranspiler, self).visit_If(node)

    def visit_BinOp(self, node):
        if (isinstance(node.left, ast.List)
                and isinstance(node.op, ast.Mult)
                and isinstance(node.right, ast.Num)):
            return "std::vector ({0},{1})".format(self.visit(node.right),
                                                  self.visit(node.left.elts[0]))
        else:
            return super(CppTranspiler, self).visit_BinOp(node)

    def visit_Module(self, node):
        lines = []
        for b in node.body:
            line = self.visit(b)
            lines.append(line)

        return "\n".join(filter(None, lines))

    def visit_alias(self, node):
        return '#include "{0}.h"'.format(node.name)

    def visit_Import(self, node):
        imports = [self.visit(n) for n in node.names]
        return "\n".join(filter(None, imports))

    def visit_List(self, node):
        if len(node.elts) > 0:
            elements = [self.visit(e) for e in node.elts]
            value_type = decltype(node.elts[0])
            return "std::vector<{0}>{{{1}}}".format(value_type,
                                                    ", ".join(elements))

        else:
            raise ValueError("Cannot create vector without elements")

    def visit_Subscript(self, node):
        if isinstance(node.slice, ast.Ellipsis):
            raise NotImplementedError('Ellipsis not supported')

        if not isinstance(node.slice, ast.Index):
            raise NotImplementedError("Advanced Slicing not supported")

        value = self.visit(node.value)
        return "{0}[{1}]".format(value, self.visit(node.slice.value))

    def visit_Tuple(self, node):
            elts = [self.visit(e) for e in node.elts]
            return "std::make_tuple({0})".format(", ".join(elts))

    def visit_TryExcept(self, node, finallybody=None):
        buf = ['try {']
        buf += [self.visit(n) for n in node.body]
        buf.append('} catch (const std::exception& e) {')

        buf += [self.visit(h) for h in node.handlers]

        if finallybody:
            buf.append('try { // finally')
            buf += [self.visit(b) for b in finallybody]
            buf.append('} throw e;')

        buf.append( '}' )

        buf.append('catch (const std::overflow_error& e) '
                   '{ std::cout << "OVERFLOW ERROR" << std::endl; }')
        buf.append('catch (const std::runtime_error& e) '
                   '{ std::cout << "RUNTIME ERROR" << std::endl; }')
        buf.append('catch (...) '
                  '{ std::cout << "UNKNOWN ERROR" << std::endl; 0}')

        return '\n'.join(buf)

    def visit_Assign(self, node):
        target = node.targets[0]

        if isinstance(target, ast.Tuple):
            elts = [self.visit(e) for e in target.elts]
            value = self.visit(node.value)
            return "std::tie({0}) = {1};".format(", ".join(elts), value)
        elif isinstance(target, ast.Name) and target.id in self._vars:
            target = self.visit(target)
            value = self.visit(node.value)
            return "{0} = {1};".format(target, value)
        elif isinstance(node.value, ast.List):
            elements = [self.visit(e) for e in node.value.elts]
            return "{0} {1} {{{2}}};".format(decltype(node),
                                            self.visit(target),
                                            ", ".join(elements))
        elif isinstance(target, ast.Subscript):
            target = self.visit(target)
            value = self.visit(node.value)
            return "{0} = {1};".format(target, value)
        else:
            target = self.visit(target)
            value = self.visit(node.value)
            self._vars.add(target)
            return "auto {0} = {1};".format(target, value)