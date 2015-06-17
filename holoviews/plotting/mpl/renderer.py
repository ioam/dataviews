import os, sys, uuid
import warnings
from io import BytesIO
from tempfile import NamedTemporaryFile

import matplotlib as mpl
from matplotlib import pyplot as plt
from matplotlib import animation
import matplotlib.tight_bbox as tight_bbox
from matplotlib.transforms import Bbox, TransformedBbox, Affine2D
from matplotlib.backends.backend_nbagg import new_figure_manager_given_figure
from mpl_toolkits.mplot3d import Axes3D

import param
from param.parameterized import bothmethod

from ...core import HoloMap, AdjointLayout
from ...core.options import Store, StoreOptions

from ..plot import Plot
from ..renderer import Renderer, MIME_TYPES
from .widgets import SelectionWidget, ScrubberWidget


class MPLRenderer(Renderer):
    """
    Exporter used to render data from matplotlib, either to a stream
    or directly to file.

    The __call__ method renders an HoloViews component to raw data of
    a specified matplotlib format.  The save method is the
    corresponding method for saving a HoloViews objects to disk.

    The save_fig and save_anim methods are used to save matplotlib
    figure and animation objects. These match the two primary return
    types of plotting class implemented with matplotlib.
    """
    drawn = {}

    backend = param.String('matplotlib', doc="The backend name.")

    fig = param.ObjectSelector(default='svg',
                               objects=['png', 'svg', 'pdf', None, 'auto'], doc="""
        Output render format for static figures. If None, no figure
        rendering will occur. """)

    holomap = param.ObjectSelector(default='gif',
                                   objects=['webm','mp4', 'gif', None, 'auto'], doc="""
        Output render multi-frame (typically animated) format. If
        None, no multi-frame rendering will occur.""")

    mode = param.ObjectSelector(default='default',
                                objects=['default', 'd3', 'nbagg'], doc="""
         The 'd3' mode uses the mpld3 library whereas the 'nbagg' uses
         matplotlib's the experimental nbagg backend. """)


    # <format name> : (animation writer, format,  anim_kwargs, extra_args)
    ANIMATION_OPTS = {
        'webm': ('ffmpeg', 'webm', {},
                 ['-vcodec', 'libvpx', '-b', '1000k']),
        'mp4': ('ffmpeg', 'mp4', {'codec': 'libx264'},
                 ['-pix_fmt', 'yuv420p']),
        'gif': ('imagemagick', 'gif', {'fps': 10}, []),
        'scrubber': ('html', None, {'fps': 5}, None)
    }

    mode_formats = {'fig':{'default': ['png', 'svg', 'pdf', None, 'auto'],
                           'd3': ['html', 'json', None, 'auto'],
                           'nbagg': ['html', None, 'auto']},
                    'holomap': {m:['webm','mp4', 'gif', None, 'auto', 'html']
                                for m in ['default', 'd3', 'nbagg']}}

    counter = 0

    # Define appropriate widget classes
    widgets = {'scrubber': ScrubberWidget,
               'selection': SelectionWidget}


    def __call__(self, obj, fmt='auto'):
        """
        Render the supplied HoloViews component or MPLPlot instance
        using matplotlib.
        """
        plot, fmt =  self._validate(obj, fmt)
        if plot is None: return

        if fmt in ['png', 'svg', 'pdf', 'html', 'json']:
            data = self._figure_data(plot, fmt, **({'dpi':self.dpi} if self.dpi else {}))
        else:
            if sys.version_info[0] == 3 and mpl.__version__[:-2] in ['1.2', '1.3']:
                raise Exception("<b>Python 3 matplotlib animation support broken &lt;= 1.3</b>")
            anim = plot.anim(fps=self.fps)
            data = self._anim_data(anim, fmt)

        return data, {'file-ext':fmt,
                      'mime_type':MIME_TYPES[fmt]}


    @classmethod
    def plot_options(cls, obj, percent_size):
        """
        Given a holoviews object and a percentage size, apply heuristics
        to compute a suitable figure size. For instance, scaling layouts
        and grids linearly can result in unwieldy figure sizes when there
        are a large number of elements. As ad hoc heuristics are used,
        this functionality is kept separate from the plotting classes
        themselves.

        Used by the IPython Notebook display hooks and the save
        utility. Note that this can be overridden explicitly per object
        using the fig_size and size plot options.
        """
        from .plot import MPLPlot
        factor = percent_size / 100.0
        obj = obj.last if isinstance(obj, HoloMap) else obj
        options = Store.lookup_options(cls.backend, obj, 'plot').options
        fig_inches = options.get('fig_inches', MPLPlot.fig_inches)
        if isinstance(fig_inches, (list, tuple)):
            fig_inches =  (fig_inches[0] * factor,
                           fig_inches[1] * factor)
        else:
            fig_inches = MPLPlot.fig_inches * factor

        return dict({'fig_inches':fig_inches},
                    **Store.lookup_options(cls.backend, obj, 'plot').options)

    @bothmethod
    def save(self_or_cls, obj, basename, fmt='auto', key={}, info={}, options=None, **kwargs):
        """
        Save a HoloViews object to file, either using an explicitly
        supplied format or to the appropriate default.
        """
        if info or key:
            raise Exception('MPLRenderer does not support saving metadata to file.')

        with StoreOptions.options(obj, options, **kwargs):
            rendered = self_or_cls(obj, fmt)
        if rendered is None: return
        (data, info) = rendered
        filename ='%s.%s' % (basename, info['file-ext'])
        with open(filename, 'wb') as f:
            f.write(self_or_cls.encode(rendered))


    @bothmethod
    def get_size(self_or_cls, plot):
        w, h = plot.state.get_size_inches()
        dpi = plot.state.dpi
        return (w*dpi, h*dpi)


    def _figure_data(self, plot, fmt='png', bbox_inches='tight', **kwargs):
        """
        Render matplotlib figure object and return the corresponding data.

        Similar to IPython.core.pylabtools.print_figure but without
        any IPython dependency.
        """
        fig = plot.state
        if self.mode == 'nbagg':
            manager = self.get_figure_manager(plot)
            if manager is None: return ''
            self.counter += 1
            manager.show()
            return ''
        elif self.mode == 'd3':
            import mpld3
            fig.dpi = self.dpi
            mpld3.plugins.connect(fig, mpld3.plugins.MousePosition(fontsize=14))
            if fmt == 'json':
                return mpld3.fig_to_dict(fig)
            else:
                return "<center>" + mpld3.fig_to_html(fig) + "<center/>"

        kw = dict(
            format=fmt,
            facecolor=fig.get_facecolor(),
            edgecolor=fig.get_edgecolor(),
            dpi=self.dpi,
            bbox_inches=bbox_inches,
        )
        kw.update(kwargs)

        # Attempts to precompute the tight bounding box
        try:
            kw = self._compute_bbox(fig, kw)
        except:
            pass

        bytes_io = BytesIO()
        fig.canvas.print_figure(bytes_io, **kw)
        data = bytes_io.getvalue()
        if fmt == 'svg':
            data = data.decode('utf-8')
        return data


    def get_figure_manager(self, plot):
        fig = plot.state
        manager = new_figure_manager_given_figure(self.counter, fig)
        # Need to call mouse_init on each 3D axis to enable rotation support
        for ax in fig.get_axes():
            if isinstance(ax, Axes3D):
                ax.mouse_init()
        return manager


    def _anim_data(self, anim, fmt):
        """
        Render a matplotlib animation object and return the corresponding data.
        """
        (writer, _, anim_kwargs, extra_args) = self.ANIMATION_OPTS[fmt]
        if extra_args != []:
            anim_kwargs = dict(anim_kwargs, extra_args=extra_args)

        anim_kwargs = dict(anim_kwargs, **({'dpi':self.dpi} if self.dpi is not None else {}))
        anim_kwargs = dict({'fps':self.fps} if fmt =='gif' else {}, **anim_kwargs)
        if not hasattr(anim, '_encoded_video'):
            with NamedTemporaryFile(suffix='.%s' % fmt) as f:
                anim.save(f.name, writer=writer,
                          **dict(anim_kwargs, **({'dpi':self.dpi} if self.dpi else {})))
                video = open(f.name, "rb").read()
        return video


    def _compute_bbox(self, fig, kw):
        """
        Compute the tight bounding box for each figure once, reducing
        number of required canvas draw calls from N*2 to N+1 as a
        function of the number of frames.

        Tight bounding box computing code here mirrors:
        matplotlib.backend_bases.FigureCanvasBase.print_figure
        as it hasn't been factored out as a function.
        """
        fig_id = id(fig)
        if kw['bbox_inches'] == 'tight' and kw['format'] == 'png':
            if not fig_id in MPLRenderer.drawn:
                fig.set_dpi(self.dpi)
                fig.canvas.draw()
                renderer = fig._cachedRenderer
                bbox_inches = fig.get_tightbbox(renderer)
                bbox_artists = fig.get_default_bbox_extra_artists()
                bbox_filtered = []
                for a in bbox_artists:
                    bbox = a.get_window_extent(renderer)
                    if a.get_clip_on():
                        clip_box = a.get_clip_box()
                        if clip_box is not None:
                            bbox = Bbox.intersection(bbox, clip_box)
                        clip_path = a.get_clip_path()
                        if clip_path is not None and bbox is not None:
                            clip_path = clip_path.get_fully_transformed_path()
                            bbox = Bbox.intersection(bbox,
                                                     clip_path.get_extents())
                    if bbox is not None and (bbox.width != 0 or
                                             bbox.height != 0):
                        bbox_filtered.append(bbox)
                if bbox_filtered:
                    _bbox = Bbox.union(bbox_filtered)
                    trans = Affine2D().scale(1.0 / self.dpi)
                    bbox_extra = TransformedBbox(_bbox, trans)
                    bbox_inches = Bbox.union([bbox_inches, bbox_extra])
                pad = plt.rcParams['savefig.pad_inches']
                bbox_inches = bbox_inches.padded(pad)
                MPLRenderer.drawn[fig_id] = bbox_inches
                kw['bbox_inches'] = bbox_inches
            else:
                kw['bbox_inches'] = MPLRenderer.drawn[fig_id]
        return kw
